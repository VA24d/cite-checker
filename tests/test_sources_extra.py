# ruff: noqa: PT009, PT027
import json
import os
import unittest
from unittest import mock

from scripts import sources
from scripts.sources import (
    ARXIV,
    CORE,
    CROSSREF,
    OPENALEX,
    UNPAYWALL,
    _crossref,
    _openalex,
    _openreview,
    _pubmed,
    get_source,
    lookup,
)

TITLE = "Dataflow Is All You Need"
LONG = TITLE + " " + "body text about dataflow. " * 80


class Fake:
    def __init__(self, pages, email=None):
        self.pages = pages
        self.offline = False
        self.cache = None
        self.email = email
        self.calls = []
        self.prefetched = {}

    def get(self, url, limit=0, web=False, data=None):
        self.calls.append(url)
        for prefix, reply in self.pages.items():
            if url.startswith(prefix):
                return reply
        return 404, b""


def j(value):
    return 200, json.dumps(value).encode()


def atom(arxiv, title):
    return (
        200,
        (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            f"<id>http://arxiv.org/abs/{arxiv}</id><title>{title}</title>"
            "<published>2021-01-01T00:00:00Z</published><summary>s</summary>"
            "<author><name>Ada Lovelace</name></author></entry></feed>"
        ).encode(),
    )


class OpenReviewTest(unittest.TestCase):
    def test_row_and_lookup(self):
        note = {
            "id": "abc",
            "forum": "abc",
            "content": {
                "title": {"value": TITLE},
                "authors": {"value": ["Ada Lovelace"]},
                "venue": {"value": "MLSys 2026"},
                "venueid": {"value": "MLSys.org/2026/Conference"},
            },
        }
        row = _openreview(note)
        self.assertEqual(row["venues"], ["MLSys 2026"])
        self.assertEqual(row["years"], [2026])
        self.assertEqual(row["url"], "https://openreview.net/forum?id=abc")
        fake = Fake({"https://api2.openreview.net": j({"notes": [note]})})
        ref = {"id": "r", "title": TITLE, "authors": ["Ada Lovelace"], "year": 2026}
        result, rows = lookup(ref, fake, {"openreview"})
        self.assertTrue(any(r["index"] == "OpenReview" for r in rows))
        self.assertTrue(result["attempts"])

    def test_empty_answer_does_not_count_as_not_found(self):
        fake = Fake({"https://api2.openreview.net": j({"notes": []})})
        result, _ = lookup({"id": "r", "title": TITLE}, fake, {"openreview"})
        self.assertEqual(result["status"], "lookup_unavailable")


class PubMedTest(unittest.TestCase):
    def test_search_then_summary(self):
        summary = {
            "uids": ["9851916"],
            "9851916": {
                "uid": "9851916",
                "title": "Genome sequence of the nematode C. elegans.",
                "authors": [{"name": "Consortium"}],
                "pubdate": "1998 Dec 11",
                "fulljournalname": "Science",
                "articleids": [
                    {"idtype": "pubmed", "value": "9851916"},
                    {"idtype": "doi", "value": "10.1126/science.282.5396.2012"},
                    {"idtype": "pmc", "value": "PMC1234"},
                ],
            },
        }
        fake = Fake(
            {
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch": j(
                    {"esearchresult": {"idlist": ["9851916"]}}
                ),
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary": j({"result": summary}),
            }
        )
        ref = {"id": "r", "title": "Genome sequence of the nematode C. elegans", "year": 1998}
        _, rows = lookup(ref, fake, {"pubmed"})
        row = rows[0]
        self.assertEqual(row["pmid"], "9851916")
        self.assertEqual(row["pmcid"], "PMC1234")
        self.assertEqual(row["doi"], "10.1126/science.282.5396.2012")
        self.assertEqual(row["years"], [1998])
        self.assertEqual(_pubmed(summary["9851916"])["venues"], ["Science"])

    def test_down_index_is_unavailable(self):
        result, _ = lookup({"id": "r", "title": TITLE}, Fake({}, ), {"pubmed"})
        self.assertEqual(result["status"], "lookup_unavailable")


def selected(row):
    return {"status": "verified", "source": row, "differences": []}


class OaTextTest(unittest.TestCase):
    def row(self):
        item = {
            "title": TITLE,
            "doi": "https://doi.org/10.1000/x",
            "best_oa_location": {"pdf_url": "https://example.org/p.pdf", "landing_page_url": "u"},
        }
        return _openalex(item)

    def test_openalex_pdf(self):
        row = self.row()
        self.assertEqual(row["oa_pdf"], "https://example.org/p.pdf")
        fake = Fake({"https://example.org/p.pdf": (200, b"%PDF-1.5 x")})
        ref = {"id": "r", "doi": "10.1000/x"}
        with mock.patch.object(sources, "local_text", return_value=LONG):
            source = get_source(ref, selected(row), [row], fake, None)
        self.assertEqual(source["level"], "full_text")
        self.assertEqual(source["origin"], "Open access PDF")

    def test_title_missing_falls_back_to_abstract(self):
        row = {**self.row(), "abstract": "short abstract"}
        fake = Fake({"https://example.org/p.pdf": (200, b"%PDF-1.5 x")})
        with mock.patch.object(sources, "local_text", return_value="other " * 400):
            source = get_source({"id": "r", "doi": "10.1000/x"}, selected(row), [row], fake, None)
        self.assertEqual(source["level"], "abstract")

    def test_no_unpaywall_without_email_and_no_core_without_key(self):
        row = {**self.row(), "oa_pdf": "", "abstract": "a"}
        fake = Fake({})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORE_API_KEY", None)
            get_source({"id": "r", "doi": "10.1000/x"}, selected(row), [row], fake, None)
        self.assertFalse(any(u.startswith(UNPAYWALL) for u in fake.calls))
        self.assertFalse(any(u.startswith(CORE) for u in fake.calls))

    def test_unpaywall_with_email(self):
        row = {**self.row(), "oa_pdf": ""}
        fake = Fake(
            {
                UNPAYWALL: j({"best_oa_location": {"url_for_pdf": "https://example.org/u.pdf"}}),
                "https://example.org/u.pdf": (200, b"%PDF-1"),
            },
            email="reader@example.org",
        )
        with mock.patch.object(sources, "local_text", return_value=LONG):
            source = get_source({"id": "r", "doi": "10.1000/x"}, selected(row), [row], fake, None)
        self.assertEqual(source["origin"], "Open access PDF")
        self.assertTrue(any(u.startswith(UNPAYWALL) for u in fake.calls))

    def test_core_with_key(self):
        row = {**self.row(), "oa_pdf": ""}
        fake = Fake(
            {
                CORE: j({"results": [{"downloadUrl": "https://example.org/c.pdf"}]}),
                "https://example.org/c.pdf": (200, b"%PDF-1"),
            }
        )
        with mock.patch.dict(os.environ, {"CORE_API_KEY": "k"}):
            with mock.patch.object(sources, "local_text", return_value=LONG):
                source = get_source({"id": "r", "doi": "10.1000/x"}, selected(row), [row], fake, None)
        self.assertEqual(source["level"], "full_text")


class NoticeTest(unittest.TestCase):
    def test_crossref_updated_by(self):
        item = {
            "DOI": "10.1000/x",
            "title": [TITLE],
            "author": [{"given": "Ada", "family": "Lovelace"}],
            "issued": {"date-parts": [[2020]]},
            "container-title": ["Journal"],
            "updated-by": [
                {"DOI": "10.1000/x.c", "type": "correction", "updated": {"date-parts": [[2021, 3, 4]]}},
                {"DOI": "10.1000/x.r", "type": "retraction", "updated": {"date-parts": [[2022, 12]]}},
                {"DOI": "10.1000/x.e", "type": "expression_of_concern"},
                {"DOI": "10.1000/x.m", "type": "new_edition"},
            ],
        }
        self.assertEqual(
            [n["kind"] for n in _crossref(item)["notices"]],
            ["correction", "retraction", "expression_of_concern"],
        )
        fake = Fake({CROSSREF + "/10.1000/x": j({"message": item})})
        ref = {"id": "r", "doi": "10.1000/x", "title": TITLE, "year": 2020, "venue": "Journal",
               "authors": ["Ada Lovelace"]}
        result, _ = lookup(ref, fake, {"crossref"})
        found = {a["kind"]: a for a in result["advisories"]}
        self.assertEqual(found["correction"]["doi"], "10.1000/x.c")
        self.assertEqual(found["correction"]["date"], "2021-03-04")
        self.assertIn("retraction", found)
        self.assertIn("expression_of_concern", found)
        self.assertNotIn("retracted", found)

    def test_update_to_retraction_flag_kept(self):
        item = {"DOI": "10.1000/n", "title": ["Retraction: x"],
                "update-to": [{"DOI": "10.1000/x", "type": "retraction"}]}
        self.assertTrue(_crossref(item)["retracted"])


class VersionTest(unittest.TestCase):
    def test_newer_version_advisory(self):
        fake = Fake(
            {
                ARXIV + "?id_list=2101.00001v2": atom("2101.00001v2", TITLE),
                ARXIV + "?id_list=2101.00001": atom("2101.00001v4", TITLE),
            }
        )
        ref = {"id": "r", "arxiv_id": "2101.00001v2", "title": TITLE, "authors": ["Ada Lovelace"],
               "year": 2021}
        result, _ = lookup(ref, fake, {"arxiv"})
        kinds = {a["kind"]: a for a in result["advisories"]}
        self.assertEqual(kinds["newer_version"]["latest"], "2101.00001v4")

    def test_current_version_no_advisory(self):
        fake = Fake({ARXIV: atom("2101.00001v2", TITLE)})
        ref = {"id": "r", "arxiv_id": "2101.00001v2", "title": TITLE, "authors": ["Ada Lovelace"],
               "year": 2021}
        result, _ = lookup(ref, fake, {"arxiv"})
        self.assertFalse([a for a in result["advisories"] if a["kind"] == "newer_version"])


if __name__ == "__main__":
    unittest.main()
