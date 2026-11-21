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
                {"numbers_absent_from_source": ["91%"]}, [p.get("hint") for p in pairs]
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
