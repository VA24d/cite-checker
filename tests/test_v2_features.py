# ruff: noqa: PT009, PT027
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from scripts.evidence import gate, packet
from scripts.shards import collect_assessments, escalations, make_shards
from scripts.sources import ARXIV, Transport, classify, prefetch_arxiv, public_url
from scripts.structure import check as structure_check
from scripts.web import check_web, cite_requests, is_web, page_metadata, parse_cff, title_match


class FakeTransport:
    def __init__(self, pages):
        self.pages = pages
        self.offline = False
        self.cache = None

    def get(self, url, limit=0, web=False):
        for prefix, reply in self.pages.items():
            if url.startswith(prefix):
                return reply
        return 404, b""


PAGE = (
    b"""<html><head><title>Scaling Laws Explained | Example Blog</title>
<meta name="citation_title" content="Scaling Laws Explained">
<meta name="citation_author" content="Ada Lovelace">
<meta name="citation_publication_date" content="2021/05/02">
<meta name="citation_doi" content="10.5555/scaling.2021">
</head><body><p>"""
    + b"Body text about scaling laws. " * 20
    + b"""</p>
<p>Please cite this work as: Lovelace (2021), doi 10.5555/scaling.2021.</p></body></html>"""
)


def ref(**fields):
    base = {
        "id": "r",
        "title": "",
        "authors": [],
        "year": None,
        "doi": "",
        "arxiv_id": "",
        "url": "",
        "raw": "",
        "venue": "",
        "authors_complete": False,
    }
    return {**base, **fields}


class Triage(unittest.TestCase):
    def test_labels_are_deterministic(self):
        self.assertEqual(classify({"status": "not_found"})["label"], "phantom")
        chimera = {
            "status": "metadata_mismatch",
            "differences": [
                {"field": "authors", "stated": ["A. Smith", "B. Jones"], "record": ["C. Wu"]}
            ],
        }
        self.assertEqual(classify(chimera)["label"], "chimeric")
        typo = {
            "status": "metadata_mismatch",
            "differences": [
                {
                    "field": "authors",
                    "stated": ["A. Smith", "B. Jones"],
                    "record": ["A. Smith", "B. Jonas"],
                }
            ],
        }
        self.assertEqual(classify(typo)["label"], "metadata_error")
        swapped = {
            "status": "metadata_mismatch",
            "differences": [{"field": "doi", "stated": "10.1/a", "record": "10.1/b"}],
        }
        self.assertEqual(classify(swapped)["label"], "chimeric")
        self.assertEqual(classify({"status": "verified"})["label"], "verified")


class WebPages(unittest.TestCase):
    def test_metadata_title_and_cite_request(self):
        page = page_metadata(PAGE)
        self.assertEqual(page["titles"][0], "Scaling Laws Explained")
        self.assertEqual(page["authors"], ["Ada Lovelace"])
        self.assertEqual(page["year"], 2021)
        self.assertEqual(page["doi"], "10.5555/scaling.2021")
        self.assertEqual(
            title_match("Scaling laws explained", ["Scaling Laws Explained | Blog"]), 1.0
        )
        self.assertEqual(cite_requests(page["text"])[0]["doi"], "10.5555/scaling.2021")

    def test_check_web_verified_with_preferred_doi(self):
        r = ref(
            title="Scaling Laws Explained",
            authors=["Lovelace, Ada"],
            year=2021,
            url="https://blog.example.org/scaling",
        )
        self.assertTrue(is_web(r))
        res, source = check_web(r, FakeTransport({"https://blog.example.org/": (200, PAGE)}))
        self.assertEqual(res["status"], "verified")
        self.assertEqual(source["origin"], "web page")
        kinds = [a["kind"] for a in res["advisories"]]
        self.assertIn("preferred_citation", kinds)
        self.assertIn("cite_request", kinds)

    def test_wrong_title_and_dead_link_with_archive(self):
        r = ref(title="A Totally Different Essay On Birds", url="https://blog.example.org/scaling")
        res, _ = check_web(r, FakeTransport({"https://blog.example.org/": (200, PAGE)}))
        self.assertEqual(res["status"], "metadata_mismatch")
        archive = json.dumps(
            {
                "archived_snapshots": {
                    "closest": {
                        "available": True,
                        "status": "200",
                        "url": "http://web.archive.org/web/2021/https://gone.example.org/x",
                    }
                }
            }
        ).encode()
        fake = FakeTransport(
            {
                "https://gone.example.org/": (404, b""),
                "https://archive.org/wayback/available": (200, archive),
                "https://web.archive.org/": (200, PAGE),
            }
        )
        res, source = check_web(
            ref(title="Scaling Laws Explained", url="https://gone.example.org/x"), fake
        )
        self.assertEqual(res["status"], "link_archived")
        self.assertEqual(classify(res)["label"], "unconfirmed")
        self.assertEqual(source["origin"], "archived web page")
        dead = FakeTransport({"https://archive.org/wayback/available": (200, b"{}")})
        res, source = check_web(ref(title="X", url="https://gone.example.org/y"), dead)
        self.assertEqual((res["status"], source), ("link_dead", None))
        self.assertEqual(classify(res)["label"], "dead_link")

    def test_citation_cff_preferred_citation(self):
        cff = parse_cff(
            "cff-version: 1.2.0\ntitle: mytool\nauthors:\n  - family-names: Lovelace\n"
            "    given-names: Ada\npreferred-citation:\n  type: article\n"
            '  title: "The Mytool Paper"\n  doi: 10.1234/mytool\n  authors:\n'
            "    - family-names: Lovelace\n    - family-names: Babbage\n  year: 2022\n"
        )
        self.assertEqual(cff["software"]["authors"], ["Lovelace"])
        self.assertEqual(cff["preferred"]["title"], "The Mytool Paper")
        self.assertEqual(cff["preferred"]["authors"], ["Lovelace", "Babbage"])
        self.assertEqual(cff["preferred"]["doi"], "10.1234/mytool")
        self.assertEqual(cff["preferred"]["year"], 2022)
        repo_page = b"<html><head><title>mytool</title></head><body>" + b"x " * 200 + b"</body>"
        fake = FakeTransport(
            {
                "https://github.com/ada/mytool": (200, repo_page),
                "https://raw.githubusercontent.com/ada/mytool/HEAD/CITATION.cff": (
                    200,
                    b"title: mytool\npreferred-citation:\n  title: The Mytool Paper\n"
                    b"  doi: 10.1234/mytool\n",
                ),
            }
        )
        res, _ = check_web(ref(title="mytool", url="https://github.com/ada/mytool"), fake)
        advice = [a for a in res["advisories"] if a["kind"] == "preferred_citation"]
        self.assertEqual(advice[0]["doi"], "10.1234/mytool")
        self.assertFalse(advice[0]["matches"])

    def test_private_addresses_are_refused(self):
        for url in (
            "http://127.0.0.1/",
            "http://10.0.0.5/x",
            "http://[::1]/",
            "file:///etc/passwd",
            "http://user:pw@example.org/",
            "http://169.254.169.254/latest",
        ):
            self.assertFalse(public_url(url), url)
        with self.assertRaises(ValueError):
            Transport(None).get("http://127.0.0.1/", web=True)


class Structure(unittest.TestCase):
    def test_structure_checks(self):
        refs = [
            ref(
                id="a",
                title="Deep Residual Learning for Images",
                doi="10.1/x",
                year=2016,
                authors=["He"],
                venue="CVPR",
                entry_type="inproceedings",
            ),
            ref(id="b", title="Deep residual learning for images", doi="10.1/x",
                entry_type="article"),
            ref(id="c", title="Some Blog Post", url="https://example.org/p", entry_type="misc"),
            ref(id="d", title="Adam", venue="ICML", authors=["Kingma"], year=2015),
            ref(
                id="e",
                title="BN",
                venue="International Conference on Machine Learning",
                authors=["Ioffe"],
                year=2015,
            ),
            ref(id="ghost"),
        ]
        claims = [{"id": "1", "text": "t", "references": ["a", "ghost", "d", "e"]}]
        found = {i["check"]: i for i in structure_check(refs, claims, manuscript=True)}
        self.assertEqual(found["undefined_citation"]["references"], ["ghost"])
        self.assertEqual(found["uncited_entry"]["references"], ["b", "c"])
        self.assertEqual(found["duplicate_entry"]["references"], ["a", "b"])
        self.assertIn("authors", found["incomplete_entry"]["message"])
        self.assertEqual(found["missing_access_date"]["references"], ["c"])
        self.assertEqual(found["inconsistent_venue"]["references"], ["d", "e"])
        ordered = [
            {"id": str(i), "text": "t", "references": [k]} for i, k in enumerate(["1", "3", "2"])
        ]
        self.assertIn("citation_order", {i["check"] for i in structure_check([], ordered)})


class Shards(unittest.TestCase):
    def _run(self, folder):
        refs = [{"id": "s", "title": "T"}, {"id": "none", "title": "U"}]
        claims = [
            {"id": "c1", "text": "Accuracy rose to 91% in trial", "references": ["s"]},
            {"id": "c2", "text": "Effect held", "references": ["s", "none"]},
        ]
        body = {
            "text": "The accuracy rose to 88% in the trial cohort. Other results follow.",
            "level": "abstract",
            "origin": "test",
            "url": "",
        }
        run = packet(refs, claims, {}, {"s": body}, [])
        sources = run.pop("sources")
        (folder / "packet.json").write_text(json.dumps(run))
        (folder / "sources.json").write_text(json.dumps(sources))
        return {**run, "sources": sources}

    def test_shards_are_compact_and_layered_answers_override(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            run = self._run(folder)
            made = make_shards(folder, size=1)
            self.assertEqual(made["pairs_sent"], 2)
            self.assertEqual(made["pairs_without_source"], 1)
            pairs = [json.loads(Path(p).read_text())["pairs"][0] for p in made["shards"]]
            self.assertEqual(set(pairs[0]) - {"hint"}, {"id", "claim", "cite", "level", "p"})
            self.assertIn(
                {"numbers_absent_from_source": ["91%"], "source_states_other_values": {"91%": ["88%"]}},
                [p.get("hint") for p in pairs]
            )
            ids = [p["id"] for p in run["pairs"] if p["reference_id"] == "s"]
            answers = folder / "assessments"
            answers.mkdir()
            (answers / "shard-01.json").write_text(
                json.dumps(
                    [
                        {"pair_id": ids[0], "verdict": "supported", "reason": "r",
                         "quotes": ["nope"]},
                        {"pair_id": ids[1], "verdict": "insufficient", "reason": "r"},
                    ]
                )
            )
            report = gate(run, collect_assessments([answers]))
            self.assertIn(ids[0], escalations(report))
            (answers / "escalate-01.json").write_text(
                json.dumps(
                    [
                        {
                            "pair_id": ids[0],
                            "verdict": "contradicted",
                            "reason": "88 not 91",
                            "quotes": ["The accuracy rose to 88% in the trial cohort."],
                        }
                    ]
                )
            )
            report = gate(run, collect_assessments([answers]))
            verdicts = {c["pair_id"]: c["verdict"] for c in report["claims"]}
            self.assertEqual(verdicts[ids[0]], "contradicted")


class ArxivBatch(unittest.TestCase):
    def test_prefetch_stores_single_id_responses(self):
        feed = b"""<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>http://arxiv.org/abs/1706.03762v7</id><title>Attention Is All You Need</title>
<published>2017-06-12T00:00:00Z</published><author><name>Ashish Vaswani</name></author>
<summary>abc</summary></entry>
<entry><id>http://arxiv.org/abs/1810.04805v2</id><title>BERT</title>
<published>2018-10-11T00:00:00Z</published><summary>x</summary></entry></feed>"""
        with TemporaryDirectory() as tmp:
            transport = Transport(Path(tmp))
            calls = []

            def fake(url, limit=0, web=False):
                calls.append(url)
                return 200, feed

            transport.get = fake
            stored = prefetch_arxiv(["1706.03762", "1810.04805", "2101.00001"], transport)
            self.assertEqual((stored, len(calls)), (2, 1))
            del transport.get
            transport.offline = True
            status, body = transport.get(ARXIV + "?" + urlencode({"id_list": "1706.03762"}))
            self.assertEqual(status, 200)
            self.assertIn(b"Attention Is All You Need", body)
            self.assertNotIn(b"BERT", body)


if __name__ == "__main__":
    unittest.main()


class Judge(unittest.TestCase):
    def test_runner_output_is_parsed_metered_and_resumable(self):
        import shlex
        import sys

        from scripts.shards import run_shards

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            Shards._run(None, folder)
            make_shards(folder, size=8)
            pair = json.loads((folder / "shards" / "shard-01.json").read_text())["pairs"][0]
            script = folder / "fake_judge.py"
            script.write_text(
                "import sys, json\n"
                f"print('Sure! ' + json.dumps([{{'pair_id': {pair['id']!r}, "
                "'verdict': 'insufficient', 'reason': 'r'}]))\n"
            )
            runner = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
            done = run_shards(folder, runner, jobs=1, backoff=0)
            self.assertEqual(done["shards"]["shard-01.json"], "ok, 1 pairs unanswered")
            self.assertEqual(done["usage"]["calls"], 1)
            self.assertTrue((folder / "meter-shard.json").exists())
            self.assertEqual(run_shards(folder, runner, backoff=0)["shards"]["shard-01.json"],
                             "cached")
            failing = run_shards(folder, "false", pattern="none-*.json", backoff=0)
            self.assertEqual(failing["shards"], {})


class SpeedAndRules(unittest.TestCase):
    def test_semantic_batch_replaces_title_match_only_on_equal_title(self):
        from scripts.sources import SEMANTIC_BATCH, lookup, prefetch_semantic

        item = {"paperId": "p1", "title": "Attention Is All You Need", "year": 2017,
                "authors": [{"name": "Ashish Vaswani"}], "externalIds": {"DOI": "10.1/att"},
                "venue": "NeurIPS"}
        calls = []

        class Fake:
            offline, cache, prefetched = False, None, {}

            def get(self, url, limit=0, web=False, data=None):
                calls.append(url.split("?")[0])
                if url.startswith(SEMANTIC_BATCH):
                    return 200, json.dumps([item, None]).encode()
                return 404, b""

        fake = Fake()
        refs = [ref(id="a", doi="10.1/att", title="Attention is all you need"),
                ref(id="b", doi="10.1/zzz", title="Other")]
        self.assertEqual(prefetch_semantic(refs, fake), 1)
        calls.clear()
        lookup(refs[0], fake, ["semanticscholar"])
        self.assertEqual(calls, [])
        lookup(refs[1], fake, ["semanticscholar"])
        self.assertEqual(len(calls), 1)

    def test_post_cache_key_and_ttl(self):
        import os
        import time

        with TemporaryDirectory() as tmp:
            t = Transport(Path(tmp), ttl_days=1)
            self.assertNotEqual(t.key("u", b"a"), t.key("u", b"b"))
            self.assertNotEqual(t.key("u"), t.key("u", b"a"))
            url = ARXIV + "?id_list=1"
            t.store(url, 200, b"<feed/>")
            self.assertEqual(t.get(url), (200, b"<feed/>"))
            old = time.time() - 3 * 86400
            os.utime(Path(tmp) / t.key(url), (old, old))
            t.offline = True  # offline replay ignores age
            self.assertEqual(t.get(url), (200, b"<feed/>"))

    def test_unrelated_title_sharing_an_author_is_not_found(self):
        from scripts.sources import identity

        cited = ref(title="Structure-aware video generation with latent diffusion models",
                    authors=["Zed Q", "Ann Blattmann"])
        record = {"index": "OpenAlex", "title": "Align Your Latents: High-Resolution Video "
                  "Synthesis with Latent Diffusion Models", "authors": ["Andreas Blattmann"],
                  "doi": "10.1/x", "years": [2023]}
        self.assertEqual(identity(cited, [record], ["OpenAlex"])["status"], "not_found")
        short = ref(title="Align your latents: high-resolution video synthesis", authors=["Andreas Blattmann"])
        self.assertNotEqual(identity(short, [record], ["OpenAlex"])["status"], "not_found")

    def test_caveat_passes_through_and_directional_insufficient_escalates(self):
        claims = [{"pair_id": "p", "claim": "Drug X reduces mortality.", "proposed_verdict":
                   "insufficient", "verdict": "insufficient", "gate_issues": [], "source": {}},
                  {"pair_id": "q", "claim": "Gene Y is expressed in liver.", "proposed_verdict":
                   "insufficient", "verdict": "insufficient", "gate_issues": [], "source": {}}]
        self.assertEqual(escalations({"claims": claims}), {"p"})
        with TemporaryDirectory() as tmp:
            run = Shards._run(None, Path(tmp))
            pair = run["pairs"][0]["id"]
            report = gate(run, [{"pair_id": pair, "verdict": "insufficient", "reason": "r",
                                 "caveat": "mice only"}])
            self.assertEqual(report["claims"][0]["caveat"], "mice only")


class ReviewFixes(unittest.TestCase):
    def test_suffix_multicast_and_nested_names(self):
        from scripts.sources import _surname
        from scripts.web import _names

        self.assertEqual(_surname("John Doe Jr."), "doe")
        self.assertEqual(_surname("Doe, John, Jr"), "doe")
        self.assertFalse(public_url("http://224.0.0.1/"))
        self.assertEqual(_names({"name": {"name": "Ada"}}), ["Ada"])
        cff = parse_cff("authors:\n  - family-names: Doe\n    affiliation:\n      name: MIT\n")
        self.assertEqual(cff["software"]["authors"], ["Doe"])


class StructuredVotes(unittest.TestCase):
    def test_derived_verdicts_and_majority(self):
        from scripts.shards import derive_verdict, vote

        base = {"pair_id": "p", "quotes": ["q"], "reason": "r"}
        cases = [
            ({"direction": "opposite"}, "contradicted"),
            ({"direction": "same", "numbers": "mismatch"}, "contradicted"),
            ({"direction": "same", "numbers": "n/a"}, "supported"),
            ({"direction": "same", "numbers": "absent"}, "partially_supported"),
            ({"direction": "same", "extra_clauses": {"x": False}}, "partially_supported"),
            ({"direction": "none"}, "insufficient"),
            ({"direction": "same", "quotes": []}, "insufficient"),
        ]
        for fields, expected in cases:
            self.assertEqual(derive_verdict({**base, **fields})["verdict"], expected, fields)
        a = [{**base, "direction": "same"}]
        b = [{**base, "direction": "opposite"}]
        merged = vote([a, a, b])[0]
        self.assertEqual((merged["verdict"], merged["split"]), ("supported", False))
        self.assertTrue(vote([a, b])[0]["split"])
        self.assertTrue(vote([a, b, [{**base, "direction": "none"}]])[0]["split"])


class CorporateAuthors(unittest.TestCase):
    def test_team_byline_is_not_compared_and_web_author_gap_is_not_chimeric(self):
        page = PAGE.replace(b"Ada Lovelace", b"Alex Gartner")
        r = ref(title="Scaling Laws Explained", authors=["Censys ARC Research Team"],
                url="https://blog.example.org/scaling")
        res, _ = check_web(r, FakeTransport({"https://blog.example.org/": (200, page)}))
        self.assertEqual(res["status"], "verified")
        web_gap = {"status": "metadata_mismatch", "source": {"index": "Web page"},
                   "differences": [{"field": "authors", "stated": ["Bo Li"], "record": ["X Y"]}]}
        self.assertEqual(classify(web_gap)["label"], "metadata_error")


class LatexExpansion(unittest.TestCase):
    def test_input_files_and_macros_are_expanded(self):
        from scripts.bibliography import read_input

        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "numbers.tex").write_text("\\newcommand{\\rate}{42\\%}\n\\def\\tool{PakoScan}\n"
                "\\newcommand{\\ui}{\\mbox{\\texttt{\\textquotesingle unsafe-inline\\textquotesingle}}\\xspace}\n"
                "\\newcommand{\\box}[2]{\\par\\fbox{\\textbf{#1} #2}\\par}\n")
            (root / "refs.bib").write_text("@article{smith2020,title={A Study},author={Smith, Ann},year={2020}}\n")
            (root / "main.tex").write_text(
                "\\input{numbers}\n\\section{Related Work}\\label{sec:rel}\n"
                "% \\cite{ignored}\n"
                "\\emph{\\tool} flagged \\rate{} of packages~\\cite{smith2020}.\n"
                "\\box{Case A.}{Sites used \\ui widely~\\cite{smith2020}.}\n"
                "\\bibliography{refs}\n"
            )
            refs, claims, _ = read_input(root / "main.tex", [])[:3]
        text = " ".join(c["text"] for c in claims)
        self.assertIn("PakoScan flagged 42% of packages", text)
        self.assertNotIn("\\label", text)
        self.assertNotIn("\\rate", text)
        self.assertIn("Sites used 'unsafe-inline' widely", text)
        self.assertNotIn("fbox", text)
        self.assertNotIn("ignored", " ".join(k for c in claims for k in c.get("keys", [])))


class EscalationLoop(unittest.TestCase):
    def test_escalated_answers_are_marked_and_not_reescalated(self):
        import argparse
        from scripts.cite_checker import _finalize

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = Shards._run(None, root)
            pair = run["pairs"][0]["id"]
            (root / "assessments").mkdir()
            (root / "assessments" / "shard-01.json").write_text(json.dumps(
                [{"pair_id": pair, "verdict": "contradicted", "reason": "r", "quotes": []}]))
            args = argparse.Namespace(run=root, assessments=[root / "assessments"], escalate_size=6)
            self.assertEqual(_finalize(args)["escalate"]["pairs"], 2)  # this pair + an unanswered one
            (root / "assessments" / "escalate-01.json").write_text(json.dumps(
                [{"pair_id": pair, "verdict": "contradicted", "reason": "r", "quotes": []}]))
            rows = collect_assessments([root / "assessments"])
            self.assertTrue(rows[0]["escalated"])
            summary = _finalize(args)
            self.assertEqual(summary["escalate"]["pairs"], 1)
            shard = json.loads(Path(summary["escalate"]["shards"][0]).read_text())
            self.assertNotIn(pair, json.dumps(shard))
            self.assertEqual(summary["escalated_answers"], 1)


class ManuscriptFeatures(unittest.TestCase):
    def test_cited_clause_and_first_person(self):
        from scripts.bibliography import _mentions

        text = (
            "Unpinned URLs (19.6% against 18.6%; odds ratio 1.07, 95% CI 0.50-2.26) show no clear "
            "difference, and crawls of deployed policies also find risky sources common \\cite{roth}. "
            "We use AIDev \\cite{li}, a public dataset of 900 pull requests opened by six agents. "
            "Most websites run third-party code, and the browser trusts it fully \\cite{nik}."
        )
        claims = {c["references"][0]: c for c in _mentions(text, latex=True)}
        self.assertEqual(claims["roth"]["text"], "crawls of deployed policies also find risky sources common")
        self.assertIn("odds ratio", claims["roth"]["sentence"])
        self.assertTrue(claims["li"]["first_person"])
        self.assertIn("six agents", claims["li"]["text"])
        self.assertNotIn("\\cite", claims["li"]["text"])
        self.assertTrue(claims["nik"]["text"].startswith("Most websites"))

    def test_secondary_citation_flag(self):
        from scripts.evidence import _attribution

        text = "Intro. Prior work found that 40% of sites use SRI [12]. We measure CSP. Our crawl found 3% of sites use SRI."
        start = text.index("40% of sites")
        self.assertTrue(_attribution(text, start, start + 10))
        own = text.index("3% of sites")
        self.assertEqual(_attribution(text, own, own + 10), "")

    def test_date_checks(self):
        from scripts.structure import date_checks

        refs = [{"id": "a", "year": 2018}, {"id": "w", "accessed": "2027-01-02", "year": 2020}]
        claims = [{"id": "c1", "text": "In March 2024 attackers took over the domain", "references": ["a"]},
                  {"id": "c2", "text": "Adoption will be broad by 2030", "references": ["a"]}]
        checks = [i["check"] for i in date_checks(refs, claims, {}, "2026-11-21")]
        self.assertEqual(checks, ["claim_after_source", "access_in_future"])


class ArxivFullText(unittest.TestCase):
    def test_cited_arxiv_text_is_used_when_index_picks_published_record(self):
        from scripts.sources import get_source

        title = "What Breaks When LLMs Code"
        published = {"index": "Crossref", "title": title, "doi": "10.1145/x", "years": [2026], "authors": []}
        preprint = {"index": "arXiv", "title": title, "arxiv_id": "2605.30777", "years": [2026], "authors": []}
        html = f"<html><head><meta name='citation_title' content='{title}'></head><body><p>{'Evasive Repair masks failures. ' * 60}</p></body></html>"
        transport = FakeTransport({"https://arxiv.org/html/2605.30777": (200, html.encode())})
        source = get_source({"id": "h", "arxiv_id": "2605.30777", "title": title},
                            {"status": "verified", "source": published, "differences": []},
                            [published, preprint], transport, Path("."))
        self.assertEqual(source["level"], "full_text")
        self.assertIn("Evasive Repair", source["text"])


class PullRequestDiff(unittest.TestCase):
    def test_pull_request_changes_are_appended(self):
        from scripts.web import _pull_request_changes

        files = json.dumps([{"filename": "index.html", "patch": "@@ -1 +1 @@\n-old\n+<script src=pako/2.1.0/pako.min.js>"}])
        transport = FakeTransport({"https://api.github.com/repos/o/r/pulls/33/files": (200, files.encode())})
        text = _pull_request_changes("https://github.com/o/r/pull/33", transport)
        self.assertIn("+<script src=pako/2.1.0/pako.min.js>", text)
        self.assertEqual(_pull_request_changes("https://github.com/o/r/issues/33", transport), "")


class SuggestCitations(unittest.TestCase):
    def test_bibliography_source_is_suggested_for_unsupported_claim(self):
        import argparse
        from scripts.cite_checker import _finalize, _suggest

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            refs = [{"id": "wrong", "title": "W"}, {"id": "right", "title": "R"}, {"id": "other", "title": "O"}]
            claims = [{"id": "c1", "text": "Subresource integrity adoption stayed below four percent of pages",
                       "references": ["wrong"]}]
            filler = "Unrelated material about compilers and billing files. " * 30
            bodies = {
                "wrong": {"text": filler, "level": "full_text", "origin": "t", "url": ""},
                "right": {"text": filler + " Subresource integrity adoption stayed below four percent of pages we crawled.",
                          "level": "full_text", "origin": "t", "url": ""},
                "other": {"text": "Gardening tips for spring. " * 40, "level": "full_text", "origin": "t", "url": ""},
            }
            run = packet(refs, claims, {}, bodies, [])
            (root / "sources.json").write_text(json.dumps(run.pop("sources")))
            (root / "packet.json").write_text(json.dumps(run))
            first = run["pairs"][0]["id"]
            folder = root / "assessments"
            folder.mkdir()
            (folder / "shard-01.json").write_text(json.dumps(
                [{"pair_id": first, "verdict": "insufficient", "reason": "Not addressed."}]))
            fin = argparse.Namespace(run=root, assessments=[folder], escalate_size=6)
            _finalize(fin)
            out = _suggest(argparse.Namespace(run=root, external=False, per_claim=3, size=8, mailto=None, cache=None))
            self.assertEqual(out["candidate_pairs"], 1)
            shard = json.loads(Path(out["shards"][0]).read_text())
            pair = shard["pairs"][0]
            packet_now = json.loads((root / "packet.json").read_text())
            self.assertEqual([p["reference_id"] for p in packet_now["pairs"] if p.get("candidate")], ["right"])
            (folder / "suggest-01.json").write_text(json.dumps([{
                "pair_id": pair["id"], "verdict": "supported", "reason": "Same finding.",
                "quotes": ["Subresource integrity adoption stayed below four percent of pages we crawled."]}]))
            summary = _finalize(fin)
            self.assertEqual(summary["suggested_citations"], 1)
            self.assertEqual(summary["pairs"], 1)
            self.assertNotIn("escalate", summary)
            report = (root / "report.md").read_text()
            self.assertIn("Already in the bibliography: right", report)

    def test_external_search_adds_suggested_reference(self):
        from scripts.suggest import suggest

        claim = "Subresource integrity adoption stayed below four percent of pages"
        words = (claim + " in a large crawl").split()
        work = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1234/sri", "title": "SRI in the wild",
                "publication_year": 2020, "authorships": [],
                "abstract_inverted_index": {w: [i] for i, w in enumerate(words)}}
        transport = FakeTransport({"https://api.openalex.org/works?": (200, json.dumps({"results": [work]}).encode())})
        run = packet([{"id": "a", "title": "A"}], [{"id": "c1", "text": claim, "references": ["a"]}], {},
                     {"a": {"text": "Gardening tips. " * 80, "level": "full_text", "origin": "t", "url": ""}}, [])
        sources = run.pop("sources")
        report = {"claims": [{"claim_id": "c1", "claim": claim, "reference_id": "a", "verdict": "insufficient"}]}
        added = suggest(run, sources, report, transport, external=True)
        self.assertEqual(len(added), 1)
        self.assertEqual(run["suggested_references"][0]["source"]["doi"], "10.1234/sri")
