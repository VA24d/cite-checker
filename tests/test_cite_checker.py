# ruff: noqa: PT009, PT027
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.bibliography import parse_bibtex, read_input
from scripts.evidence import gate, packet, rank
from scripts.sources import get_source, identity

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "cite-checker"


class CitationChecks(unittest.TestCase):
    def test_bibtex_nested_macros_concatenation_and_comments(self):
        source = """% @article{ignored, title={Ignore}}
@string{conf = "ICLR"}
@article(x, title={A {Nested} "Title"}, author={García, Ana and Doe, Jane},
    year=2024, booktitle=conf # " Workshop", doi={https://doi.org/10.1234/TEST})"""
        rows = parse_bibtex(source)
        self.assertEqual([row["id"] for row in rows], ["x"])
        self.assertEqual(rows[0]["venue"], "ICLR Workshop")
        self.assertEqual(rows[0]["title"], 'A Nested "Title"')
        self.assertEqual(rows[0]["doi"], "10.1234/test")

    def test_markdown_numbered_ranges_and_pandoc_mentions(self):
        source = """Finding A [1\u20133]. Finding B [@foo].

## References
[1] First work, 2024. https://doi.org/10.1234/abc
[2] Second work, 2023.
[3] Third work, 2022.
"""
        with TemporaryDirectory() as folder:
            path = Path(folder) / "paper.md"
            path.write_text(source, encoding="utf-8")
            refs, claims, notices = read_input(path, [])
        self.assertEqual([r["id"] for r in refs], ["1", "2", "3", "foo"])
        self.assertEqual(claims[0]["references"], ["1", "2", "3"])
        self.assertTrue(notices)

    def test_empty_indices_and_ambiguous_results_abstain(self):
        ref = {"id": "x", "title": "An exact title of a paper"}
        self.assertEqual(identity(ref, [], [])["status"], "lookup_unavailable")
        records = [
            {"title": ref["title"], "doi": f"10.1234/{i}", "index": "Crossref"} for i in (1, 2)
        ]
        self.assertEqual(identity(ref, records, ["Crossref"])["status"], "ambiguous")

    def test_exact_identifier_requires_metadata_and_fuzzy_is_review_only(self):
        ref = {"id": "x", "title": "An exact title of a paper", "doi": "10.1234/a"}
        candidate = {"title": "A completely unrelated work", "doi": ref["doi"], "index": "Crossref"}
        self.assertEqual(identity(ref, [candidate], ["Crossref"])["status"], "metadata_mismatch")
        self.assertEqual(identity(ref, [], ["Crossref"])["status"], "not_found")

    def test_replay_rejects_wrong_source_stitched_and_changed_quotes(self):
        refs = [{"id": "a"}, {"id": "b"}]
        claims = [{"id": "c", "text": "Both groups improved.", "references": ["a", "b"]}]
        bodies = {
            "a": {
                "text": "Group A improved in the trial. It lasted twelve weeks.",
                "level": "abstract",
            },
            "b": {"text": "Group B failed to improve during the full trial.", "level": "local"},
        }
        run = packet(refs, claims, {}, bodies, [])
        left, right = run["pairs"]
        proposals = [
            {
                "pair_id": left["id"],
                "verdict": "supported",
                "reason": "test",
                "quotes": ["Group B failed to improve during the full trial."],
            },
            {
                "pair_id": right["id"],
                "verdict": "contradicted",
                "reason": "test",
                "quotes": ["Group B failed to improve during the full trial."],
            },
        ]
        result = gate(run, proposals)
        self.assertEqual([r["verdict"] for r in result["claims"]], ["insufficient", "contradicted"])
        span = result["claims"][1]["evidence"][0]
        self.assertEqual(bodies["b"]["text"][span["start"] : span["end"]], span["quote"])
        proposals[0]["quotes"] = ["Group A improved ... lasted twelve weeks."]
        self.assertEqual(gate(run, proposals)["claims"][0]["verdict"], "insufficient")
        run["sources"]["b"]["text"] += "!"
        with self.assertRaisesRegex(ValueError, "digest"):
            gate(run, proposals)

    def test_passage_offsets_are_stable(self):
        source = "Unrelated material. " * 120 + "In the main trial, 24 adults showed recurrence."
        rows = rank(source, "24 adults showed recurrence", 2)
        self.assertIn("24 adults", rows[0]["text"])
        for row in rows:
            self.assertEqual(source[row["start"] : row["end"]], row["text"])
        self.assertEqual(rows, rank(source, "24 adults showed recurrence", 2))

    def test_short_abstract_is_presented_intact_even_with_one_passage(self):
        text = "The study measured recurrence. " * 60 + "At week 8, recurrence was higher."
        refs = [{"id": "study"}]
        claims = [{"id": "c", "text": "The trial reduced recurrence.", "references": ["study"]}]
        body = {"level": "abstract", "text": text}
        run = packet(refs, claims, {}, {"study": body}, [], top=1)
        self.assertEqual(
            run["pairs"][0]["passages"], [{"text": text, "start": 0, "end": len(text)}]
        )
        full = packet(refs, claims, {}, {"study": {**body, "level": "full_text"}}, [], top=1)
        self.assertLess(len(full["pairs"][0]["passages"][0]["text"]), len(text))

    def test_identifier_title_word_and_omitted_author_are_detected(self):
        citation = """@article{trial, title={Estimating Response from Regional Medical Records},
            author={Ada Lee and Bo Chen}, doi={10.1234/trial}}"""
        ref = parse_bibtex(citation)[0]
        row = {
            "title": "Estimating Recovery from Regional Medical Records",
            "authors": ["Ada Lee", "Bo Chen", "Clara Shah"],
            "doi": ref["doi"],
            "index": "Crossref",
        }
        result = identity(ref, [row], ["Crossref"])
        self.assertEqual({d["field"] for d in result["differences"]}, {"title", "authors"})
        abbreviated = parse_bibtex(citation.replace("Bo Chen", "others"))[0]
        result = identity(abbreviated, [row], ["Crossref"])
        self.assertEqual([d["field"] for d in result["differences"]], ["title"])

    def test_versions_venue_aliases_and_preprint_doi(self):
        authors = ["Ada Lee", "Bo Chen"]
        title = "Sparse Estimators for Streaming Graphs"
        preprint = {
            "index": "OpenAlex",
            "title": title,
            "authors": authors,
            "doi": "10.48550/arxiv.2101.00001",
            "arxiv_id": "2101.00001",
            "years": [2021],
            "venues": ["arXiv (Cornell University)"],
        }
        paper = {
            "index": "Crossref",
            "title": title,
            "authors": authors,
            "doi": "10.1234/conf.42",
            "years": [2022],
            "venues": ["Proceedings of the AAAI Conference on Artificial Intelligence"],
        }
        semantic = {
            "index": "Semantic Scholar",
            "title": title,
            "authors": authors,
            "arxiv_id": "2101.00001",
            "years": [2022],
            "venues": ["International Conference on Machine Learning"],
            "venue_aliases": ["ICML", "Int Conf Mach Learn"],
        }
        cited = {"id": "a", "title": title, "authors": authors, "year": 2022}
        result = identity({**cited, "venue": "AAAI"}, [preprint, paper], ["OpenAlex", "Crossref"])
        self.assertEqual(result["status"], "verified")
        result = identity(
            {**cited, "venue": "ICML", "doi": preprint["doi"], "arxiv_id": "2101.00001"},
            [preprint, semantic],
            ["OpenAlex", "Semantic Scholar"],
        )
        self.assertEqual(result["status"], "verified")
        result = identity({**cited, "venue": "COLT"}, [preprint, semantic], ["OpenAlex"])
        self.assertEqual([d["field"] for d in result["differences"]], ["venue"])

    def test_future_year_unregistered_doi_and_unrelated_authors_are_flagged(self):
        record = {
            "index": "OpenAlex",
            "title": "Sparse Estimators for Streaming Graphs",
            "authors": ["Ada Lee", "Bo Chen"],
            "arxiv_id": "2101.00001",
            "years": [2021],
            "venues": ["arXiv (Cornell University)"],
        }
        ref = {"id": "a", "title": record["title"], "authors": record["authors"], "venue": "ICLR"}
        self.assertEqual(
            identity({**ref, "year": 2022}, [record], ["OpenAlex"])["status"],
            "metadata_unconfirmed",
        )
        future = identity({**ref, "year": 2091}, [record], ["OpenAlex"])
        self.assertEqual([d["field"] for d in future["differences"]], ["year"])
        fake = {**ref, "year": 2021, "doi": "10.5555/not-real"}
        self.assertEqual(identity(fake, [record], ["OpenAlex"])["status"], "metadata_unconfirmed")
        flagged = identity(fake, [record], ["OpenAlex"], unregistered_doi=True)
        self.assertEqual([d["field"] for d in flagged["differences"]], ["doi"])
        invented = {
            **ref,
            "title": "Sparse Estimators for Streaming Hypergraphs",
            "authors": ["Kim Ito"],
        }
        self.assertEqual(identity(invented, [record], ["OpenAlex"])["status"], "not_found")
        renamed = identity({**invented, "authors": ["Ada Lee"]}, [record], ["OpenAlex"])
        self.assertEqual([d["field"] for d in renamed["differences"]], ["title"])
        other = {
            **record,
            "title": "Sparse Estimators for Streaming Multigraphs",
            "arxiv_id": "2202.2",
        }
        tie = identity({**invented, "authors": ["Ada Lee"]}, [record, other], ["OpenAlex"])
        self.assertEqual(tie["status"], "ambiguous")

    def test_duplicate_quote_requires_offset_and_claim_judgment_stays_separate(self):
        quote = "This sentence appears in several places."
        text = (
            quote + "\nWhere it refers to a protocol.\n" + quote + "\nWhere it refers to results."
        )
        run = packet(
            [{"id": "a"}],
            [{"id": "c", "text": "The claim is about the results.", "references": ["a"]}],
            {},
            {"a": {"text": text, "level": "full_text"}},
            [],
        )
        row = {
            "pair_id": run["pairs"][0]["id"],
            "verdict": "supported",
            "reason": "test",
            "quotes": [quote],
        }
        self.assertEqual(gate(run, [])["claims"][0]["verdict"], "not_assessed")
        self.assertEqual(gate(run, [row])["claims"][0]["verdict"], "insufficient")
        row["quotes"] = [{"text": quote, "start": text.rfind(quote)}]
        self.assertEqual(gate(run, [row])["claims"][0]["verdict"], "supported")
        row["quotes"][0]["start"] += 1
        self.assertEqual(gate(run, [row])["claims"][0]["verdict"], "insufficient")
        row["quotes"][0] = {"text": quote, "start": 0, "sha256": "0" * 64}
        self.assertEqual(gate(run, [row])["claims"][0]["verdict"], "insufficient")
        changed = packet(
            [{"id": "a"}],
            [{"id": "c", "text": "A different claim.", "references": ["a"]}],
            {},
            {"a": {"text": text, "level": "full_text"}},
            [],
        )
        with self.assertRaisesRegex(ValueError, "Unknown or duplicate pair"):
            gate(changed, [row])

    def test_local_packet_reports_missing_text_and_a_skipped_metadata_lookup(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / "audit"
            cli = [sys.executable, "-B", str(SKILL / "scripts" / "cite_checker.py")]
            fixture = ROOT / "benchmarks" / "fixtures" / "closed-book.json"
            subprocess.run(
                [*cli, "prepare", str(fixture), "--out", str(run), "--local-only"],
                check=True,
                capture_output=True,
            )
            subprocess.run([*cli, "finalize", str(run)], check=True, capture_output=True)
            first = (run / "report.json").read_bytes()
            report = json.loads(first)
            self.assertEqual(len(report["claims"]), 10)
            self.assertEqual(report["claims"][3]["verdict"], "insufficient")
            self.assertIn("intentionally skipped", (run / "report.md").read_text(encoding="utf-8"))
            subprocess.run([*cli, "finalize", str(run)], check=True, capture_output=True)
            self.assertEqual((run / "report.json").read_bytes(), first)

    def test_installer_is_idempotent_and_preserves_another_installation(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            cmd = [sys.executable, "-B", str(ROOT / "install.py"), "--project", str(root)]
            subprocess.run(cmd, check=True, capture_output=True)
            subprocess.run(cmd, check=True, capture_output=True)
            for name in (".agents", ".claude", ".gemini"):
                installed = root / name / "skills" / "cite-checker"
                self.assertEqual(
                    (installed / "SKILL.md").read_bytes(), (SKILL / "SKILL.md").read_bytes()
                )
                subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(installed / "scripts" / "cite_checker.py"),
                        "--help",
                    ],
                    check=True,
                    capture_output=True,
                )
            wrapper = root / ".gemini" / "commands" / "cite-checker.toml"
            self.assertIn("{{args}}", wrapper.read_text(encoding="utf-8"))
            wrapper.write_text("custom command", encoding="utf-8")
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 2)
            self.assertEqual(wrapper.read_text(encoding="utf-8"), "custom command")

    def test_pinned_arxiv_never_uses_a_different_revision(self):
        class MissingText:
            def __init__(self):
                self.urls = []

            def get(self, url, limit=None):
                self.urls.append(url)
                return 404, b""

        ref = {"id": "a", "arxiv_id": "2401.01234v1"}
        row = {
            "arxiv_id": "2401.01234v2",
            "title": "Example study",
            "index": "arXiv",
            "abstract": "Abstract from the later version.",
        }
        result = {"status": "metadata_unconfirmed", "source": row}
        transport = MissingText()
        self.assertIsNone(get_source(ref, result, [row], transport, ROOT))
        self.assertEqual(
            transport.urls,
            ["https://arxiv.org/html/2401.01234v1", "https://arxiv.org/pdf/2401.01234v1"],
        )
        pinned = {**row, "arxiv_id": "2401.01234v1", "abstract": "Version one abstract."}
        self.assertEqual(
            get_source(
                ref, {**result, "source": pinned}, [pinned], transport, ROOT, abstract_only=True
            )["text"],
            pinned["abstract"],
        )

    def test_repository_parsed_paper_shape_and_narrative_links(self):
        assembled = {
            "bibliography": [{"id": "b1", "title": "An example paper"}],
            "sentences": [{"id": "s1", "text": "The estimate was 13% [1]."}],
            "citations": [{"id": "c1", "sentence_id": "s1", "bib_ids": ["b1"]}],
        }
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "paper.json"
            path.write_text(json.dumps(assembled), encoding="utf-8")
            _, claims, _ = read_input(path, [])
            self.assertEqual(
                claims, [{"id": "c1", "text": "The estimate was 13% [1].", "references": ["b1"]}]
            )
            path = root / "paper.md"
            path.write_text(
                "As shown in [1], the response was 13%. [2] used a different regimen.",
                encoding="utf-8",
            )
            _, claims, _ = read_input(path, [])
            self.assertEqual([c["references"] for c in claims], [["1"], ["2"]])
            self.assertIn("response was 13%", claims[0]["text"])
            self.assertIn("a different regimen", claims[1]["text"])

    def test_publication_doi_keeps_publication_text_and_conflicts_abstain(self):
        class NoLookup:
            def get(self, url, limit=None):
                raise AssertionError(f"unexpected alternate version fetch: {url}")

        ref = {"id": "a", "doi": "10.1234/published"}
        row = {
            "doi": ref["doi"],
            "arxiv_id": "2401.01234v2",
            "index": "OpenAlex",
            "title": "The final article",
            "abstract": "The published abstract.",
        }
        resolved = identity(ref, [row], ["OpenAlex"])
        self.assertEqual(
            get_source(ref, resolved, [row], NoLookup(), ROOT)["text"], row["abstract"]
        )
        other = {
            "arxiv_id": "2301.01234v1",
            "title": "A completely different paper",
            "index": "arXiv",
            "abstract": "An unrelated abstract.",
        }
        double = {**ref, "arxiv_id": other["arxiv_id"]}
        resolved = identity(double, [row, other], ["OpenAlex", "arXiv"])
        self.assertEqual(resolved["status"], "ambiguous")

    def test_close_but_different_title_requires_review_without_identifier(self):
        name = "Study of long-term recurrence after intervention in patients with stage II disease"
        ref = {"id": "a", "title": name}
        other = {"index": "OpenAlex", "title": name.replace("II", "III"), "doi": "10.1234/other"}
        self.assertEqual(identity(ref, [other], ["OpenAlex"])["status"], "ambiguous")

    def test_pdf_page_breaks_and_inline_latex_references(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "pdf-layout.txt"
            path.write_text(
                "We measured this outcome [1].\n\fReferences\n"
                "[1] Example citation. https://doi.org/10.1234/article\n"
                "\nAppendix\nUnpublished notes should stay outside the bibliography.",
                encoding="utf-8",
            )
            refs, _, _ = read_input(path, [])
            self.assertEqual(refs[0]["doi"], "10.1234/article")
            self.assertNotIn("Unpublished", refs[0]["raw"])
            path = root / "paper.tex"
            path.write_text(
                r"We measured this outcome \cite{ref}."
                r"\begin{thebibliography}{9}\bibitem{ref} Example."
                r"\url{https://doi.org/10.1234/article}\end{thebibliography}",
                encoding="utf-8",
            )
            refs, claims, _ = read_input(path, [])
            self.assertEqual(refs[0]["doi"], "10.1234/article")
            self.assertEqual(claims[0]["references"], ["ref"])
