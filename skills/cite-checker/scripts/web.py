"""Check cited web pages and software: liveness, author-declared metadata, preferred citation.

Everything here is deterministic. Pages are fetched once, cached like index responses,
and compared with the cited entry by string rules only.
"""

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlencode, urlsplit

from scripts.bibliography import arxiv_of, doi_of, local_text
from scripts.sources import _Page, _similar, _surname, _title, _year

SCHOLARLY_HOSTS = {"doi.org", "dx.doi.org", "arxiv.org", "www.arxiv.org", "export.arxiv.org"}
WAYBACK = "https://archive.org/wayback/available"
DEAD = {404, 410, 451}
TITLE_META = ("citation_title", "dc.title", "og:title", "twitter:title")
AUTHOR_META = ("citation_author", "dc.creator", "author", "article:author")
DATE_META = (
    "citation_publication_date",
    "citation_date",
    "citation_online_date",
    "dc.date",
    "article:published_time",
)
VENUE_META = ("citation_journal_title", "citation_conference_title", "og:site_name")
# Corporate bylines ("Censys ARC Research Team") are not people and rarely match a page's
# author meta tag, which names the individual writer.
ORGANIZATION = re.compile(
    r"(?i)\b(?:team|group|inc|ltd|llc|lab|labs|laboratory|research|foundation|corporation|"
    r"corp|company|project|contributors|consortium|committee|association|institute|"
    r"university|agency|council|organi[sz]ation|security|staff|editors|authors)\b"
)
CITE_REQUEST = re.compile(
    r"(?i)\b(?:please\s+cite|how\s+to\s+cite|if\s+you\s+use\s+[^.]{0,80}?\bcite|"
    r"cite\s+(?:this|our)\s+(?:work|paper|software|dataset))"
)


class _Meta(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.ld = []
        self.title = ""
        self.h1 = ""
        self._in = None
        self._buffer = []

    def handle_starttag(self, name, attrs):
        values = {k.lower(): (v or "") for k, v in attrs}
        if name == "meta":
            key = (values.get("name") or values.get("property") or "").lower()
            if key and values.get("content"):
                self.meta.setdefault(key, []).append(values["content"].strip())
        elif name == "script" and "ld+json" in values.get("type", ""):
            self._in, self._buffer = "ld", []
        elif name in ("title", "h1") and not getattr(self, name):
            self._in, self._buffer = name, []

    def handle_endtag(self, name):
        if self._in == "ld" and name == "script":
            self.ld.append("".join(self._buffer))
            self._in = None
        elif self._in == name and name in ("title", "h1"):
            setattr(self, name, " ".join("".join(self._buffer).split()))
            self._in = None

    def handle_data(self, data):
        if self._in:
            self._buffer.append(data)


def _ld_records(blobs):
    out = []
    for blob in blobs:
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if node.get("headline") or node.get("name"):
                    out.append(node)
                stack.extend(v for k, v in node.items() if k in ("@graph", "mainEntity"))
    return out


def _names(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        name = value.get("name")
        return _names(name) if isinstance(name, (dict, list)) else [str(name)] if name else []
    if isinstance(value, list):
        return [n for item in value for n in _names(item)]
    return []


def _pick(meta, keys):
    return next((meta[k][0] for k in keys if meta.get(k)), "")


def page_metadata(content):
    """Return the author-declared metadata and readable text of an HTML page."""
    markup = content.decode("utf-8", errors="replace")
    meta = _Meta()
    meta.feed(markup)
    reader = _Page()
    reader.feed(markup)
    text = "\n\n".join(
        " ".join(part.split()) for part in "".join(reader.parts).split("\n\n") if part.strip()
    )
    titles = [meta.meta[k][0] for k in TITLE_META if meta.meta.get(k)]
    authors = next((meta.meta[k] for k in AUTHOR_META if meta.meta.get(k)), [])
    date = _pick(meta.meta, DATE_META)
    for record in _ld_records(meta.ld):
        titles.append(str(record.get("headline") or record.get("name") or ""))
        authors = authors or _names(record.get("author"))
        date = date or str(record.get("datePublished") or "")
    titles += [t for t in (meta.h1, meta.title) if t]
    return {
        "titles": list(dict.fromkeys(t.strip() for t in titles if t.strip())),
        "authors": [a for a in dict.fromkeys(authors) if a],
        "year": _year(date),
        "venue": _pick(meta.meta, VENUE_META),
        "doi": doi_of(_pick(meta.meta, ("citation_doi", "dc.identifier", "prism.doi"))),
        "arxiv_id": arxiv_of(_pick(meta.meta, ("citation_arxiv_id",)), bare=True),
        "formal": bool(meta.meta.get("citation_title")),
        "text": text,
    }


def title_match(stated, candidates):
    want = _title(stated)
    best = 0.0
    for candidate in candidates:
        # Page titles often carry a site suffix: "Title | Site" or "Title - Site".
        for piece in [candidate, *re.split(r"\s+[|–—-]\s+", candidate)]:
            have = _title(piece)
            if not want or not have:
                continue
            if want == have or (len(want) > 12 and want in have):
                return 1.0
            best = max(best, _similar(want, have))
    return best


def cite_requests(text, limit=3):
    """Short snippets where a page asks to be cited in a particular way."""
    found = []
    for match in CITE_REQUEST.finditer(text):
        snippet = " ".join(text[match.start() : match.start() + 500].split())
        found.append({"text": snippet, "doi": doi_of(snippet), "arxiv_id": arxiv_of(snippet)})
        if len(found) >= limit:
            break
    return found


def parse_cff(text):
    """Read title, DOI, year and family names from CITATION.cff, both the top level and
    preferred-citation. Covers the plain block YAML the format uses in practice."""
    blocks = {"": {"authors": []}}
    section, in_authors = "", False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        body = line[2:] if line.startswith("- ") else line
        key, _, value = body.partition(":")
        key, value = key.strip(), value.strip().strip("'\"")
        if indent == 0:
            section = "preferred" if key == "preferred-citation" else ""
            blocks.setdefault(section, {"authors": []})
            in_authors = key == "authors"
            if section == "" and value:
                blocks[""][key] = value
            continue
        block = blocks[section]
        if section == "preferred" and indent <= 2 and not line.startswith("- "):
            in_authors = key == "authors"
            if value and not in_authors:
                block[key] = value
            continue
        # "name" counts only as a list item; a nested affiliation also has a name key.
        if in_authors and (key == "family-names" or (key == "name" and line.startswith("- "))):
            block["authors"].append(value)
    result = {}
    for name, block in blocks.items():
        if block.get("title") or block["authors"]:
            result["preferred" if name else "software"] = {
                "title": block.get("title", ""),
                "authors": block["authors"],
                "doi": doi_of(block.get("doi", "")),
                "year": _year(block.get("year") or block.get("date-released")),
            }
    return result


def github_repo(url):
    parts = urlsplit(url)
    bits = [b for b in parts.path.split("/") if b]
    if (parts.hostname or "").lower() in ("github.com", "www.github.com") and len(bits) >= 2:
        return bits[0], bits[1].removesuffix(".git")
    return None


def cff_advisory(ref, cff):
    target = cff.get("preferred") or cff.get("software")
    if not target:
        return None
    title_ok = title_match(ref.get("title", ""), [target["title"]]) >= 0.9
    doi_ok = not target["doi"] or target["doi"] == ref.get("doi")
    return {
        "kind": "preferred_citation",
        "message": "The authors' CITATION.cff asks to be cited as this work."
        if "preferred" in cff
        else "The authors' CITATION.cff describes this software.",
        "title": target["title"],
        "authors": target["authors"],
        "doi": target["doi"],
        "year": target["year"],
        "matches": title_ok and doi_ok,
    }


def _archived(url, transport, near_year=None):
    params = {"url": url}
    if near_year:
        params["timestamp"] = f"{near_year}0101"
    status, content = transport.get(WAYBACK + "?" + urlencode(params))
    if status != 200:
        return None
    try:
        closest = json.loads(content)["archived_snapshots"]["closest"]
    except (ValueError, KeyError, TypeError):
        return None
    if not closest.get("available") or not str(closest.get("status", "")).startswith("2"):
        return None
    return closest.get("url", "").replace("http://", "https://", 1)


def is_web(ref):
    url = ref.get("url") or ""
    host = (urlsplit(url).hostname or "").lower()
    return url.startswith(("http://", "https://")) and host not in SCHOLARLY_HOSTS


def _fetch(url, transport):
    try:
        return transport.get(url, limit=6_000_000, web=True)
    except ValueError as error:
        return -1, str(error).encode()


def _document(content):
    if not content.startswith(b"%PDF-"):
        return page_metadata(content)
    with TemporaryDirectory() as directory:
        path = Path(directory) / "page.pdf"
        path.write_bytes(content)
        try:
            text = local_text(path)
        except (ValueError, OSError, subprocess.SubprocessError):
            text = ""
    head = text[:600]
    return {
        "titles": [head.strip().split("\n", 1)[0]] if head.strip() else [],
        "authors": [],
        "year": None,
        "venue": "",
        "doi": doi_of(head),
        "arxiv_id": arxiv_of(head),
        "formal": False,
        "text": text,
        "head": head,
    }


def check_web(ref, transport):
    """Resolve a reference through its URL. Returns (resolution, source-or-None)."""
    url = ref["url"]
    attempts, advisories, differences = [], [], []
    status, content = _fetch(url, transport)
    attempts.append({"index": "web", "mode": "page", "http_status": status if status > 0 else None})
    result = {
        "id": ref["id"],
        "reference": {k: v for k, v in ref.items() if k not in ("text", "path")},
        "looked_up": ["web"],
        "source": None,
        "differences": differences,
        "candidates": [],
        "advisories": advisories,
        "attempts": attempts,
    }
    fetched_from = url
    # -1 means the host did not resolve to a public address (or did not resolve at all).
    unreachable = status in DEAD or status == -1
    if status != 200 and (unreachable or status in (0, 403)):
        snapshot = _archived(url, transport, ref.get("year"))
        attempts.append(
            {"index": "Wayback Machine", "mode": "archive", "http_status": 200 if snapshot else None}
        )
        if snapshot:
            status, content = _fetch(snapshot, transport)
            fetched_from = snapshot
            if status == 200:
                advisories.append(
                    {
                        "kind": "archived_copy",
                        "message": "The live page is unavailable; cite the archived copy.",
                        "url": snapshot,
                    }
                )
        elif unreachable:
            return dict(result, status="link_dead"), None
    if status != 200:
        return dict(result, status="lookup_unavailable"), None
    page = _document(content)
    result["source"] = {
        "index": "Web page",
        "title": page["titles"][0] if page["titles"] else "",
        "authors": page["authors"],
        "years": [page["year"]] if page["year"] else [],
        "venues": [page["venue"]] if page["venue"] else [],
        "doi": page["doi"],
        "arxiv_id": page["arxiv_id"],
        "url": fetched_from,
    }
    stated_title = ref.get("title") or ""
    score = title_match(stated_title, page["titles"] + ([page["head"]] if "head" in page else []))
    in_text = _title(stated_title) in _title(page["text"][:3000]) if stated_title else False
    if stated_title and page["titles"] and score < 0.6 and not in_text:
        differences.append({"field": "title", "stated": stated_title, "record": page["titles"][0]})
    people = [a for a in ref.get("authors", []) if not ORGANIZATION.search(a)]
    if people and page["authors"]:
        cited = {_surname(a) for a in people} - {"al", "others", ""}
        if cited and not cited & {_surname(a) for a in page["authors"]}:
            differences.append(
                {"field": "authors", "stated": ref["authors"], "record": page["authors"]}
            )
    if page["formal"] and ref.get("year") and page["year"] and abs(ref["year"] - page["year"]) > 1:
        differences.append({"field": "year", "stated": ref["year"], "record": [page["year"]]})
    if page["doi"] and page["doi"] != ref.get("doi"):
        advisories.append(
            {
                "kind": "preferred_citation",
                "message": "The page declares a DOI; cite the work by it.",
                "doi": page["doi"],
                "matches": False,
            }
        )
    for request in cite_requests(page["text"]):
        matches = (not request["doi"] or request["doi"] == ref.get("doi")) and (
            not request["arxiv_id"] or request["arxiv_id"] == ref.get("arxiv_id")
        )
        advisories.append({"kind": "cite_request", **request, "matches": matches})
    if repo := github_repo(url):
        owner, name = (quote(part, safe="") for part in repo)
        cff_url = f"https://raw.githubusercontent.com/{owner}/{name}/HEAD/CITATION.cff"
        cff_status, cff_body = _fetch(cff_url, transport)
        attempts.append(
            {"index": "CITATION.cff", "mode": "preferred", "http_status": max(cff_status, 0) or None}
        )
        if cff_status == 200:
            advice = cff_advisory(ref, parse_cff(cff_body.decode("utf-8", "replace")))
            if advice:
                advisories.append(advice)
    if differences:
        status = "metadata_mismatch"
    elif fetched_from != url:
        status = "link_archived"
    elif stated_title and (score >= 0.85 or in_text):
        status = "verified"
    else:
        status = "metadata_unconfirmed"
    source = None
    if len(page["text"]) >= 200:
        source = {
            "text": page["text"],
            "level": "full_text",
            "url": fetched_from,
            "origin": "archived web page" if fetched_from != url else "web page",
        }
    return dict(result, status=status), source
