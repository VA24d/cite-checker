# ruff: noqa: PT009, PT027
import json
import unittest

from scripts.registry import check, check_claims, mentions

NPM = {
    "dist-tags": {"latest": "3.0.3"},
    "versions": {"2.1.0": {}, "3.0.3": {}},
    "time": {"2.1.0": "2022-11-07T01:02:09.895Z"},
}
POLYJS = {"dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": {}}, "time": {}}
PYPI = {
    "info": {"version": "2.34.2"},
    "releases": {"2.31.0": [{"upload_time_iso_8601": "2023-05-22T15:12:42.313790Z"}], "2.34.2": []},
}
CRATE = {
    "crate": {"max_version": "1.0.229"},
    "versions": [{"num": "1.0.229", "created_at": "2026-07-18T23:05:13Z"}],
}


class FakeTransport:
    def __init__(self, pages):
        self.pages = pages
        self.offline = False
        self.cache = None
        self.calls = []

    def get(self, url, limit=0, web=False):
        self.calls.append(url)
        for prefix, reply in self.pages.items():
            if url.startswith(prefix):
                return reply
        return 404, b""


def j(obj):
    return 200, json.dumps(obj).encode()


def one(text):
    found = mentions(text)
    assert len(found) == 1, found
    return found[0]


class MentionTests(unittest.TestCase):
    def test_npm_at_version(self):
        m = one("Copilot gave pako@2.1.0 for the task.")
        self.assertEqual((m["ecosystem"], m["name"], m["version"]), ("npm", "pako", "2.1.0"))
        self.assertEqual("pako@2.1.0", "Copilot gave pako@2.1.0 for the task."[slice(*m["span"])])
        self.assertFalse(m["asserts_missing"])

    def test_scoped(self):
        m = one("It pinned @types/node@20.1.0 in package.json.")
        self.assertEqual((m["name"], m["version"]), ("@types/node", "20.1.0"))

    def test_pypi_forms(self):
        self.assertEqual(one("Pinned requests==2.31.0 in the lockfile.")["ecosystem"], "pypi")
        m = one("Run pip install flask to start.")
        self.assertEqual((m["ecosystem"], m["name"]), ("pypi", "flask"))
        m = one("The Python package numpy@1.26.0 was used.")
        self.assertEqual(m["ecosystem"], "pypi")

    def test_crates_and_github(self):
        self.assertEqual(one("Use cargo add serde@1.0.229 here.")["ecosystem"], "crates")
        m = one("See https://github.com/python/cpython.git for details.")
        self.assertEqual((m["ecosystem"], m["name"]), ("github", "python/cpython"))

    def test_phrases(self):
        self.assertEqual(one("We used the npm package lodash.")["name"], "lodash")
        m = one("The `left-pad` package on npm is tiny.")
        self.assertEqual((m["ecosystem"], m["name"]), ("npm", "left-pad"))

    def test_polybool_assertion(self):
        found = mentions(
            "Agents also referenced 3 packages that do not exist on npm, "
            "such as polybool instead of polybooljs."
        )
        by = {m["name"]: m for m in found}
        self.assertTrue(by["polybool"]["asserts_missing"])
        self.assertFalse(by["polybooljs"]["asserts_missing"])
        self.assertEqual(by["polybool"]["ecosystem"], "npm")

    def test_false_positives(self):
        for text in [
            "As shown by [@smith2020] the effect holds.",
            "Contact alice@example.com or bob@2.example.org.",
            "Accuracy rose from 1.2 to 3.4 in version 2.0 of the benchmark.",
            "See @smith2020 and @jones2021 for details.",
        ]:
            self.assertEqual(mentions(text), [], text)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.t = FakeTransport({
            "https://registry.npmjs.org/pako": j(NPM),
            "https://registry.npmjs.org/polybooljs": j(POLYJS),
            "https://registry.npmjs.org/@types%2Fnode": j(POLYJS),
            "https://pypi.org/pypi/requests/json": j(PYPI),
            "https://crates.io/api/v1/crates/serde": j(CRATE),
            "https://api.github.com/repos/python/cpython": j({"full_name": "python/cpython"}),
            "https://registry.npmjs.org/flaky": (500, b""),
        })

    def mk(self, eco, name, ver=None, missing=False):
        return {"ecosystem": eco, "name": name, "version": ver, "asserts_missing": missing}

    def test_npm_version_exists(self):
        r = check(self.mk("npm", "pako", "2.1.0"), self.t)
        self.assertEqual(r["status"], "version_exists")
        self.assertEqual((r["latest"], r["published"]), ("3.0.3", "2022-11-07T01:02:09.895Z"))
        self.assertFalse(r["conflict"])

    def test_npm_version_missing(self):
        r = check(self.mk("npm", "pako", "9.9.9"), self.t)
        self.assertEqual(r["status"], "version_missing")
        self.assertTrue(r["conflict"])

    def test_scoped_url(self):
        check(self.mk("npm", "@types/node"), self.t)
        self.assertIn("https://registry.npmjs.org/@types%2Fnode", self.t.calls)

    def test_pypi(self):
        r = check(self.mk("pypi", "requests", "2.31.0"), self.t)
        self.assertEqual(r["status"], "version_exists")
        self.assertTrue(r["published"].startswith("2023-05-22"))

    def test_crate_and_github(self):
        self.assertEqual(check(self.mk("crates", "serde", "1.0.229"), self.t)["status"], "version_exists")
        self.assertEqual(check(self.mk("github", "python/cpython"), self.t)["status"], "exists")
        self.assertEqual(check(self.mk("github", "no/such"), self.t)["status"], "missing")

    def test_asserts_missing_conflict(self):
        r = check(self.mk("npm", "polybooljs", missing=True), self.t)
        self.assertEqual(r["status"], "exists")
        self.assertTrue(r["conflict"])
        r = check(self.mk("npm", "polybool", missing=True), self.t)
        self.assertEqual((r["status"], r["conflict"]), ("missing", False))

    def test_404_vs_500(self):
        self.assertEqual(check(self.mk("npm", "nope"), self.t)["status"], "missing")
        r = check(self.mk("npm", "flaky"), self.t)
        self.assertEqual((r["status"], r["conflict"]), ("lookup_unavailable", False))

    def test_check_claims_dedupes(self):
        claims = [
            {"id": "c1", "text": "Copilot gave pako@2.1.0."},
            {"id": "c2", "text": "Also pako@2.1.0 appeared."},
            {"id": "c3", "text": "Agents cited packages that do not exist on npm, such as polybool instead of polybooljs."},
        ]
        out = check_claims(claims, self.t)
        self.assertEqual([f["id"] for f in out], ["c1", "c2", "c3", "c3"])
        self.assertEqual(self.t.calls.count("https://registry.npmjs.org/pako"), 1)
        poly = {f["name"]: f for f in out if f["id"] == "c3"}
        self.assertEqual(poly["polybool"]["status"], "missing")
        self.assertFalse(poly["polybool"]["conflict"])
        self.assertEqual(poly["polybooljs"]["status"], "exists")


if __name__ == "__main__":
    unittest.main()
