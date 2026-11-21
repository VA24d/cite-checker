"""Keyless scholarly lookup, conservative identity matching, and source retrieval."""

import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts.bibliography import REF_HEADING, arxiv_of, doi_of, local_text

CROSSREF = "https://api.crossref.org/works"
OPENALEX = "https://api.openalex.org/works"
ARXIV = "https://export.arxiv.org/api/query"
EUROPE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
SEMANTIC = "https://api.semanticscholar.org/graph/v1/paper/search/match"
SEMANTIC_FIELDS = "title,authors,year,venue,publicationVenue,journal,externalIds"
SEMANTIC_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
HANDLES = "https://doi.org/api/handles/"
DATACITE = "https://api.datacite.org/dois/"
OPENREVIEW = "https://api2.openreview.net/notes/search"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
UNPAYWALL = "https://api.unpaywall.org/v2/"
CORE = "https://api.core.ac.uk/v3/search/works"
HOSTS = {
    "api.crossref.org",
    "api.openalex.org",
    "export.arxiv.org",
    "arxiv.org",
    "www.ebi.ac.uk",
    "api.semanticscholar.org",
    "doi.org",
    "api.datacite.org",
    "archive.org",
    "api2.openreview.net",
    "eutils.ncbi.nlm.nih.gov",
    "api.unpaywall.org",
    "api.core.ac.uk",
}
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
# Allowed distance between a cited venue year and the first-version years when no
# published-version record is available. Conference papers usually appear within a
# year of the preprint. Journal versions can take several years, so they stay open.
YEAR_BEFORE = 1
CONFERENCE_YEAR_AFTER = 1
NEAR_TITLE = 0.85
INDEX_NAMES = {
    "crossref": "Crossref",
    "openalex": "OpenAlex",
    "arxiv": "arXiv",
    "europepmc": "Europe PMC",
    "semanticscholar": "Semantic Scholar",
    "datacite": "DataCite",
    "openreview": "OpenReview",
    "pubmed": "PubMed",
}
# Indices that never count toward the lookup quorum: they add candidates but a
# successful empty answer from them is not evidence that a work is missing.
EXTRA_INDICES = {"openreview", "pubmed"}
VENUES = {
    "international conference on learning representations": "iclr",
    "international conference on machine learning": "icml",
    "advances in neural information processing systems": "neurips",
    "neural information processing systems": "neurips",
    "nips": "neurips",
    "association for computational linguistics": "acl",
    "empirical methods in natural language processing": "emnlp",
    "computer vision and pattern recognition": "cvpr",
    "international conference on computer vision": "iccv",
    "european conference on computer vision": "eccv",
    "journal of machine learning research": "jmlr",
    "j mach learn res": "jmlr",
    "mach learn": "machine learning",
    "transactions of the association for computational linguistics": "tacl",
}


def _allowed(url):
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname in HOSTS
        and parts.port in (443, None)
        and not (parts.username or parts.password)
    )


def public_url(url):
    """True for an http(s) URL whose host resolves only to public addresses."""
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.port not in (None, 80, 443)
    ):
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    try:
        addresses = [ipaddress.ip_address(i[4][0]) for i in infos]
        return bool(addresses) and all(a.is_global and not a.is_multicast for a in addresses)
    except ValueError:
        return False


class _Redirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed(newurl):
            raise URLError("redirect outside supported scholarly sources")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _WebRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_url(newurl):
            raise URLError("redirect to a non-public address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Transport:
    def __init__(self, cache, offline=False, email=None, ttl_days=None):
        self.cache = cache
        self.offline = offline
        self.email = email
        self.opener = build_opener(_Redirect())
        self.web_opener = build_opener(_WebRedirect())
        self.last = {}
        self._lock = threading.Lock()
        # Cached answers older than this are fetched again in live mode (None: never).
        self.ttl = ttl_days * 86400 if ttl_days else None
        # Records fetched in bulk ahead of per-reference lookups, keyed by identifier.
        self.prefetched = {}

    def _pace(self, host, delay):
        # Reserve the next slot for this host under a lock, then sleep outside it,
        # so worker threads share each host's rate limit but different hosts overlap.
        with self._lock:
            slot = max(time.monotonic(), self.last.get(host, 0) + delay)
            self.last[host] = slot
        time.sleep(max(0, slot - time.monotonic()))

    def get(self, url, limit=8_000_000, web=False, data=None):
        if web:
            if not self.offline and not public_url(url):
                raise ValueError("URL is not a public http(s) address")
        elif not _allowed(url):
            raise ValueError("URL is outside the supported scholarly sources")
        key = self.key(url, data)
        path = self.cache / key if self.cache else None
        stale = (
            path is not None
            and self.ttl is not None
            and not self.offline
            and path.exists()
            and time.time() - path.stat().st_mtime > self.ttl
        )
        if path is not None and path.exists() and not stale:
            cached = path.read_bytes()
            status, _, content = cached.partition(b"\n")
            if (status in (b"200", b"404") or (self.offline and status.isdigit())) and len(
                content
            ) <= limit:
                return int(status), content
        if self.offline:
            return 0, b""
        host = urlsplit(url).hostname
        headers = {"User-Agent": "cite-checker/2.0 (citation verification)"}
        if web:
            headers["Accept"] = "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.email:
            headers["User-Agent"] += f" (mailto:{self.email})"
        semantic = host == "api.semanticscholar.org"
        if semantic and os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
            headers["x-api-key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"]
        # Keys travel only with the live request, never in the cache key.
        fetch = url
        if host == "eutils.ncbi.nlm.nih.gov" and os.environ.get("NCBI_API_KEY"):
            separator = "&" if urlsplit(url).query else "?"
            fetch = url + separator + urlencode({"api_key": os.environ["NCBI_API_KEY"]})
        if host == "api.core.ac.uk" and os.environ.get("CORE_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["CORE_API_KEY"]
        if host == "api.openalex.org" and os.environ.get("OPENALEX_API_KEY"):
            separator = "&" if urlsplit(url).query else "?"
            fetch = url + separator + urlencode({"api_key": os.environ["OPENALEX_API_KEY"]})
        # Pace arXiv's API at its published three-second interval. Semantic Scholar's
        # keyless pool is shared and often returns 429, so back off longer there.
        # NCBI allows 3 requests/s without a key and 10/s with one.
        ncbi = 0.11 if os.environ.get("NCBI_API_KEY") else 0.35
        delay = {
            "export.arxiv.org": 3,
            "api.semanticscholar.org": 1.1,
            "eutils.ncbi.nlm.nih.gov": ncbi,
        }.get(host, 0.15)
        tries = 6 if semantic else 2
        opener = self.web_opener if web else self.opener
        for attempt in range(tries):
            self._pace(host, delay)
            try:
                with opener.open(Request(fetch, data=data, headers=headers), timeout=20) as response:
                    content = response.read(limit + 1)
                    status = 200 if len(content) <= limit else 413
            except HTTPError as error:
                status, content = error.code, b""
            except (URLError, TimeoutError, http.client.HTTPException, ConnectionError):
                status, content = 0, b""
            if status not in (0, 429, 500, 502, 503, 504):
                break
            if attempt < tries - 1:
                time.sleep(min(2**attempt, 16))
        # Freeze failure receipts for offline replay but retry them in live mode.
        self.store(url, status, content, data)
        return status, content

    @staticmethod
    def key(url, data=None):
        if data is not None:
            url += "\nPOST " + hashlib.sha256(data).hexdigest()
        return hashlib.sha256(url.encode()).hexdigest()

    def store(self, url, status, content, data=None):
        if self.cache is None:
            return
        key = self.key(url, data)
        path = self.cache / key
        path.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=path.parent) as work:
            staging = Path(work) / key
            staging.write_bytes(str(status).encode() + b"\n" + (content if status == 200 else b""))
            staging.replace(path)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = []
        self.title = ""
        self.page_title = []  # <title> text, used when there is no citation_title
        self.in_title = False

    def handle_starttag(self, name, attrs):
        values = dict(attrs)
        self.in_title = name == "title"
        role = (values.get("class") or "") + " " + (values.get("id") or "")
        if name == "meta" and values.get("name") == "citation_title":
            self.title = values.get("content") or ""
        if name not in VOID_TAGS and (
            self.ignored
            or name in ("script", "style", "nav", "footer", "head", "noscript")
            or re.search(r"\b(?:ltx_bibliography|ltx_biblist|references)\b", role)
        ):
            self.ignored.append(name)
        if not self.ignored and name in ("p", "div", "section", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n\n")
        elif not self.ignored and name in ("td", "th", "br"):
            self.parts.append(" ")

    def handle_endtag(self, name):
        if name == "title":
            self.in_title = False
            if not self.title:
                self.title = " ".join("".join(self.page_title).split())
        if self.ignored and self.ignored[-1] == name:
            self.ignored.pop()

    def handle_data(self, data):
        if self.in_title:
            self.page_title.append(data)
        if not self.ignored:
            self.parts.append(data)


def plain(value):
    parser = _Page()
    parser.feed(value or "")
    return " ".join("".join(parser.parts).split())


def words(text):
    text = unicodedata.normalize("NFKD", html.unescape(text).casefold())
    return re.findall(r"[^\W_]+", "".join(c for c in text if not unicodedata.combining(c)))


def _title(text):
    return " ".join(words(text))


def _surname(name):
    name = re.sub(r"(?i),?\s+(?:jr|sr|ii|iii|iv)\.?$", "", name.strip())
    family = name.split(",")[0] if "," in name else re.sub(r"(?:\s+[A-Z]\.)+$", "", name)
    bits = words(family)
    return bits[-1] if bits else ""


def _similar(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _venue(text):
    label = " ".join(w for w in words(text) if not w.isdigit()).strip()
    for long, abbreviation in sorted(VENUES.items(), key=lambda row: -len(row[0])):
        if label == long or (long in label and "workshop" not in label):
            return abbreviation
    return re.sub(r"\b(?:proceedings|of|the|annual|meeting|ieee|acm)\b", "", label).strip()


def _arxiv_base(value):
    return re.sub(r"v\d+$", "", value or "")


def _year(value):
    match = re.search(r"\b(?:19|20)\d{2}\b", str(value or ""))
    return int(match[0]) if match else None


def _location(row):
    return row.get("url") or (f"https://doi.org/{row['doi']}" if row.get("doi") else "")


def _scores(ref, row):
    claimed = _title(ref.get("title", ""))
    found = _title(row.get("title", ""))
    claimed_author = _surname((ref.get("authors") or [""])[0])
    authors = {_surname(name) for name in row.get("authors", [])}
    first_matches = bool(claimed_author and claimed_author in authors)
    exact_doi = bool(ref.get("doi") and ref["doi"] == row.get("doi"))
    exact_arxiv = bool(
        ref.get("arxiv_id") and _arxiv_base(ref["arxiv_id"]) == _arxiv_base(row.get("arxiv_id"))
    )
    similarity = _similar(claimed, found)
    if (
        not claimed
        and ref.get("raw")
        and len(found.split()) >= 5
        and f" {found} " in f" {_title(ref['raw'])} "
    ):
        similarity = 1.0
    return (exact_doi or exact_arxiv, similarity, first_matches)


def _preprint_doi(doi):
    return doi.startswith("10.48550/arxiv.")


def _same_record(a, b):
    if a.get("doi") and a.get("doi") == b.get("doi"):
        return True
    if a.get("arxiv_id") and b.get("arxiv_id"):
        return _arxiv_base(a["arxiv_id"]) == _arxiv_base(b["arxiv_id"])
    # A preprint DOI and a publication DOI can name two versions of one work.
    if a.get("doi") and b.get("doi") and not (_preprint_doi(a["doi"]) or _preprint_doi(b["doi"])):
        return False
    gaps = [abs(x - y) for x in a.get("years") or [] for y in b.get("years") or []]
    return (
        a.get("index") != b.get("index")
        and _title(a["title"]) == _title(b["title"])
        and bool(a.get("authors") and b.get("authors"))
        and _surname(a["authors"][0]) == _surname(b["authors"][0])
        and (not gaps or min(gaps) <= 1)
    )


def _name_in(surname, pool):
    """Surname match that tolerates transliteration and typos: Ebrahimian and
    Ebrahimianghazani, Ren and Rent, Yu-Qing and Yuqing."""
    if surname in pool:
        return True
    return any(
        len(surname) >= 3
        and len(other) >= 3
        and (
            other.startswith(surname)
            or surname.startswith(other)
            or (_similar(surname, other) >= 0.85 and surname[0] == other[0])
        )
        for other in pool
    )


def _shares_author(ref, row):
    cited = {_surname(name) for name in ref.get("authors", [])} - {"al", "others", ""}
    return bool(cited & {_surname(name) for name in row.get("authors", [])})


def identity(ref, rows, completed, unregistered_doi=False, min_indices=1):
    identifier = ref.get("doi") or ""
    arxiv_id = _arxiv_base(ref.get("arxiv_id"))
    ranked = sorted(
        (row for row in rows if row.get("title")),
        key=lambda row: (
            bool(identifier and row.get("doi") == identifier),
            *_scores(ref, row),
            row.get("index") != "Semantic Scholar",
            bool(row.get("doi")),
            row.get("index") == "Crossref",
            _location(row),
        ),
        reverse=True,
    )
    result = {
        "id": ref["id"],
        "reference": {k: v for k, v in ref.items() if k not in ("text", "path")},
        "looked_up": sorted(set(completed)),
        "source": None,
        "differences": [],
        "candidates": [],
    }
    if not any(ref.get(k) for k in ("doi", "arxiv_id", "title", "raw")):
        return dict(result, status="insufficient_metadata")
    # "Not found" needs enough indices to have answered. A rate-limited index is silence,
    # and silence must not turn a real work into a phantom.
    absent = "not_found" if len(set(completed)) >= min_indices else "lookup_unavailable"
    if not ranked:
        return dict(result, status=absent if completed else "lookup_unavailable")
    best = ranked[0]
    doi_record = next((r for r in ranked if identifier and r.get("doi") == identifier), None)
    arxiv_record = next(
        (r for r in ranked if arxiv_id and _arxiv_base(r.get("arxiv_id")) == arxiv_id),
        None,
    )
    if not doi_record and _preprint_doi(identifier) and arxiv_record:
        doi_record = arxiv_record
    if (
        doi_record
        and arxiv_record
        and _similar(_title(doi_record["title"]), _title(arxiv_record["title"])) < 0.9
    ):
        return dict(
            result, status="ambiguous", candidates=[_public(doi_record), _public(arxiv_record)]
        )
    exact, similarity, first_matches = _scores(ref, best)
    # Records join through any shared DOI, arXiv ID, or matching title and first author,
    # so a preprint, its publication, and an aggregator entry form one group.
    peers, pending = [best], ranked[1:]
    while joined := [r for r in pending if any(_same_record(peer, r) for peer in peers)]:
        peers += joined
        pending = [r for r in pending if r not in joined]
    tied = [
        r
        for r in ranked[1:]
        if r not in peers
        and _scores(ref, r) >= (exact, max(0.93, similarity - 0.02), first_matches)
    ]
    # One clearly closest title with the same first author is the cited work with a
    # changed title, not an unidentified reference.
    rivals = [r for r in ranked if r not in peers and _scores(ref, r)[1:] >= (NEAR_TITLE, True)]
    near = not tied and not rivals and first_matches and similarity >= NEAR_TITLE
    plausible = exact or similarity == 1 or near
    if (
        not plausible
        and not tied
        and ref.get("authors")
        and not any(_shares_author(ref, r) for r in ranked[:3])
    ):
        return dict(result, status=absent, candidates=[_public(r) for r in ranked[:3]])
    # A cited title that is neither close to (>= NEAR_TITLE) nor a shortened form of any
    # indexed title names no indexed work, even when a candidate shares an author.
    cited, closest = _title(ref.get("title", "")), _title(best["title"])
    shortened = len(cited.split()) >= 4 and (
        f" {cited} " in f" {closest} " or f" {closest} " in f" {cited} "
    )
    if tied or (
        not plausible
        and similarity >= 0.72
        and (similarity >= NEAR_TITLE or shortened or not cited)
    ):
        return dict(result, status="ambiguous", candidates=[_public(r) for r in ranked[:3]])
    if not plausible:
        return dict(result, status=absent, candidates=[_public(r) for r in ranked[:3]])
    improvements = {
        "years": sorted({y for r in peers for y in r.get("years", [])}),
        "venues": list(dict.fromkeys(v for r in peers for v in r.get("venues", []))),
        "venue_aliases": list(dict.fromkeys(v for r in peers for v in r.get("venue_aliases", []))),
        "venue_types": sorted({t for r in peers for t in r.get("venue_types", [])}),
        "retracted": any(r.get("retracted") for r in peers),
        "notices": _notice_list(r for p in peers for r in p.get("notices", [])),
    }
    source = {**best, **improvements}
    differences = []
    if identifier and not doi_record and best.get("doi") and not arxiv_record:
        differences.append({"field": "doi", "stated": ref["doi"], "record": best["doi"]})
    elif identifier and not doi_record and unregistered_doi:
        record = best.get("doi") or "no DOI registered at doi.org"
        differences.append({"field": "doi", "stated": ref["doi"], "record": record})
    explicit_arxiv = ref.get("arxiv_id", "")
    if explicit_arxiv and not arxiv_record and best.get("arxiv_id"):
        differences.append(
            {"field": "arxiv_id", "stated": explicit_arxiv, "record": best["arxiv_id"]}
        )
    wanted_title = _title(ref.get("title", ""))
    known_title = _title(best["title"])
    word_substitution = (
        exact
        and len(wanted_title.split()) == len(known_title.split())
        and wanted_title.replace(" ", "") != known_title.replace(" ", "")
    )
    if wanted_title and (similarity < 0.97 or word_substitution):
        differences.append({"field": "title", "stated": ref["title"], "record": best["title"]})
    authors = best.get("authors", [])
    claimed_authors = ref.get("authors", [])
    author_doubt = False
    if authors and claimed_authors:
        have = {_surname(name) for name in authors} - {""}
        listed = {_surname(name) for r in peers for name in r.get("authors", [])} - {""}
        cited = {_surname(name) for name in claimed_authors}
        abbreviated = bool(cited & {"al", "others"})
        cited -= {"al", "others", ""}
        extra = {name for name in cited if not _name_in(name, listed)}
        omitted = {name for name in have if not _name_in(name, cited)}
        complete = ref.get("authors_complete") and not abbreviated
        # A dropped co-author is checked when the work is pinned by identifier, or by an
        # identical title and first author with at least a quarter of the authors missing.
        dropped = complete and not extra and omitted and (
            exact
            or (similarity >= 0.97 and first_matches and len(omitted) / len(have) >= 0.25)
        )
        # One unmatched name in a long, otherwise matching list is usually an index error
        # (a merged record or a misspelt name), so it needs review rather than a flag.
        index_noise = len(extra) == 1 and len(cited) >= 4 and len(cited - extra) >= 3
        if (extra and not index_noise) or dropped:
            differences.append({"field": "authors", "stated": claimed_authors, "record": authors})
        elif index_noise:
            author_doubt = True
    venue = _venue(ref.get("venue", ""))
    # Only acronym aliases such as ICML. Abbreviations like "Int Conf Mach Learn" collapse
    # to generic labels that match unrelated venues.
    acronyms = [a for a in source["venue_aliases"] if re.fullmatch(r"[A-Za-z]{3,12}", a)]
    names = source["venues"] + acronyms
    known = [_venue(v) for v in names if v]
    non_preprint = [v for v in known if v and "arxiv" not in v and "preprint" not in v]
    years = source["years"]
    # Semantic Scholar and preprint records give the first version's year, so a cited
    # venue year can only be bounded when no published-version record was found.
    published = [
        r
        for r in peers
        if r["index"] != "Semantic Scholar"
        and any(
            "arxiv" not in _venue(v) and "preprint" not in _venue(v) for v in r.get("venues", [])
        )
    ]
    stated = ref.get("year")
    if stated and years and stated not in years:
        bounded = venue and not published and "arxiv" not in venue and venue != "corr"
        if (
            not bounded
            or stated > time.gmtime().tm_year
            or stated < min(years) - YEAR_BEFORE
            or (
                "conference" in source["venue_types"]
                and stated > max(years) + CONFERENCE_YEAR_AFTER
            )
        ):
            differences.append({"field": "year", "stated": stated, "record": years})
    if (
        venue
        and non_preprint
        and venue not in known
        and all(f" {venue} " not in f" {v} " and f" {v} " not in f" {venue} " for v in non_preprint)
        and not (explicit_arxiv and "arxiv" in venue)
    ):
        differences.append({"field": "venue", "stated": ref["venue"], "record": source["venues"]})
    incomplete = bool(
        author_doubt
        or (identifier and not doi_record and (not best.get("doi") or arxiv_record))
        or (
            explicit_arxiv
            and (
                not arxiv_record
                or (
                    re.search(r"v\d+$", explicit_arxiv)
                    and explicit_arxiv != arxiv_record["arxiv_id"]
                )
            )
        )
        or (ref.get("year") and not years)
        or (claimed_authors and not authors)
        or (venue and not (non_preprint or venue in known))
        or (ref.get("raw") and not wanted_title)
    )
    status = (
        "metadata_mismatch" if differences else "metadata_unconfirmed" if incomplete else "verified"
    )
    advisories = []
    cites_preprint = (not venue or "arxiv" in venue or venue == "corr") and (
        explicit_arxiv or _preprint_doi(identifier) or "arxiv" in venue or venue == "corr"
    )
    if cites_preprint and published:
        version = published[0]
        advisories.append(
            {
                "kind": "published_version",
                "message": "A peer-reviewed version exists; most authors ask to cite it.",
                "venue": next(
                    (v for v in version.get("venues", []) if "arxiv" not in _venue(v)), ""
                ),
                "doi": version.get("doi", ""),
                "years": version.get("years", []),
            }
        )
    if source["retracted"]:
        advisories.append({"kind": "retracted", "message": "An index marks this work retracted."})
    advisories.extend(_notice_advisories(source.get("notices", [])))
    advisories.extend(_version_advisory(explicit_arxiv, arxiv_record))
    return dict(
        result,
        status=status,
        source=_public(source),
        differences=differences,
        advisories=advisories,
    )


# Deterministic triage labels, derived only from the identity result.
CLASS_ORDER = (
    "phantom",
    "chimeric",
    "metadata_error",
    "dead_link",
    "needs_review",
    "unconfirmed",
    "unresolved",
    "verified",
    "not_assessed",
)


def _overlap(stated, record):
    cited = {_surname(n) for n in stated or []} - {"al", "others", ""}
    known = {_surname(n) for n in record or []}
    return len(cited & known) / len(cited) if cited else 1.0


def classify(resolution):
    """Label a reference resolution without any model judgment.

    phantom: every completed index lookup failed to find the work.
    chimeric: a real work was found, but its identifier points elsewhere or most of the
      cited authors are not its authors, so the entry splices two records together.
    metadata_error: the work is identified, with smaller title, year, venue or author errors.
    """
    status = resolution.get("status")
    fields = {d["field"]: d for d in resolution.get("differences", [])}
    reasons = []
    if status == "not_found":
        label = "phantom"
        reasons.append("No index holds a work with this title and authors.")
    elif status == "link_dead":
        label = "dead_link"
        reasons.append("The cited URL no longer resolves and no archived copy was found.")
    elif status == "metadata_mismatch":
        authors = fields.get("authors")
        overlap = _overlap(authors["stated"], authors["record"]) if authors else 1.0
        swapped_id = bool({"doi", "arxiv_id"} & fields.keys())
        # A web page byline is weak evidence of authorship, so a mismatch there is an
        # error to fix, not a spliced record.
        web = (resolution.get("source") or {}).get("index") == "Web page"
        if (overlap < 0.5 and not web) or swapped_id:
            label = "chimeric"
            if overlap < 0.5:
                reasons.append(f"Only {overlap:.0%} of the cited authors wrote the matched work.")
            if swapped_id:
                unregistered = "no DOI registered" in str(fields.get("doi", {}).get("record", ""))
                reasons.append(
                    "The cited DOI is not registered at doi.org."
                    if unregistered
                    else "The cited identifier belongs to a different record."
                )
        else:
            label = "metadata_error"
            reasons.append("Fields differ from the record: " + ", ".join(sorted(fields)) + ".")
    elif status == "ambiguous":
        label = "needs_review"
        reasons.append("Several records fit equally well.")
    elif status in ("metadata_unconfirmed", "link_archived"):
        label = "unconfirmed"
    elif status in ("lookup_unavailable", "insufficient_metadata"):
        label = "unresolved"
    elif status == "verified":
        label = "verified"
    else:
        label = "not_assessed"
    return {"label": label, "reasons": reasons}


def _public(row):
    return {
        k: v for k, v in row.items() if k not in ("abstract", "pmcid", "notices", "oa_pdf")
    }


NOTICE_KINDS = {
    "correction": "correction",
    "corrigendum": "correction",
    "erratum": "erratum",
    "expression_of_concern": "expression_of_concern",
    "retraction": "retraction",
}


def _notice_list(entries):
    seen, out = set(), []
    for entry in entries:
        key = (entry["kind"], entry.get("doi", ""), entry.get("date", ""))
        if key not in seen:
            seen.add(key)
            out.append(entry)
    return out


def _crossref_notices(item):
    """Notices from Crossref's updated-by list (editorial updates to this work)."""
    found = []
    for entry in item.get("updated-by") or []:
        kind = NOTICE_KINDS.get(str(entry.get("type") or "").lower().replace("-", "_"))
        if not kind:
            continue
        parts = (entry.get("updated") or {}).get("date-parts", [[]])[0] or []
        date = "-".join(f"{int(n):02d}" if i else str(n) for i, n in enumerate(parts[:3]))
        found.append({"kind": kind, "doi": doi_of(entry.get("DOI")), "date": date})
    return found


def _notice_advisories(notices):
    labels = {
        "correction": "A correction notice is attached to this work.",
        "erratum": "An erratum is attached to this work.",
        "expression_of_concern": "An expression of concern is attached to this work.",
        "retraction": "A retraction notice is attached to this work.",
    }
    return [
        {
            "kind": n["kind"],
            "message": labels[n["kind"]],
            "doi": n.get("doi", ""),
            "date": n.get("date", ""),
        }
        for n in notices
    ]


def _version_number(value):
    match = re.search(r"v(\d+)$", value or "")
    return int(match[1]) if match else None


def _version_advisory(requested, record):
    record = record or {}
    newest = record.get("latest_arxiv") or record.get("arxiv_id")
    pinned, latest = _version_number(requested), _version_number(newest)
    if pinned and latest and latest > pinned and _arxiv_base(requested) == _arxiv_base(newest):
        return [
            {
                "kind": "newer_version",
                "message": f"The cited arXiv version v{pinned} has a newer version (v{latest}); check that the claims still hold in it.",
                "latest": newest,
            }
        ]
    return []


def _crossref(item):
    dates = [
        item.get(key, {}).get("date-parts", [[]])[0] or [None]
        for key in ("issued", "published", "published-online", "published-print")
    ]
    authors = [
        " ".join(v for v in (a.get("given"), a.get("family") or a.get("name")) if v)
        for a in item.get("author", [])
    ]
    updates = item.get("update-to", [])
    retraction = any(u.get("type") == "retraction" for u in updates)
    return {
        "index": "Crossref",
        "title": plain((item.get("title") or [""])[0]),
        "authors": authors,
        "doi": doi_of(item.get("DOI")),
        "url": item.get("URL") or "",
        "years": sorted({y[0] for y in dates if y[0]}),
        "venues": item.get("container-title") or [],
        "abstract": plain(item.get("abstract")),
        "retracted": retraction,
        "notices": _crossref_notices(item),
    }


def _datacite(item):
    attrs = item.get("attributes", {})
    authors = [
        " ".join(v for v in (c.get("givenName"), c.get("familyName")) if v) or c.get("name", "")
        for c in attrs.get("creators", [])
    ]
    container = (attrs.get("container") or {}).get("title") or attrs.get("publisher") or ""
    if isinstance(container, dict):
        container = container.get("name", "")
    abstract = next(
        (d.get("description", "") for d in attrs.get("descriptions", [])
         if d.get("descriptionType") == "Abstract"),
        "",
    )
    year = _year(attrs.get("publicationYear"))
    kind = (attrs.get("types") or {}).get("resourceTypeGeneral") or ""
    return {
        "index": "DataCite",
        "title": plain((attrs.get("titles") or [{}])[0].get("title", "")),
        "authors": [a for a in authors if a],
        "doi": doi_of(attrs.get("doi")),
        "url": attrs.get("url") or "",
        "years": [year] if year else [],
        "venues": [container] if container else [],
        "venue_types": [kind.lower()] if kind else [],
        "abstract": plain(abstract),
        "retracted": False,
    }


def _openalex(item):
    inverted = item.get("abstract_inverted_index") or {}
    sequence = sorted((pos, token) for token, indices in inverted.items() for pos in indices)
    locations = [item.get(k) or {} for k in ("primary_location", "best_oa_location")]
    venues = [location.get("source") or {} for location in locations]
    names = [v["display_name"] for v in venues if v.get("display_name")]
    urls = [location.get("landing_page_url") or "" for location in locations]
    arxiv = next((a for url in urls if (a := arxiv_of(url))), "")
    ids = item.get("ids") or {}
    oa = item.get("best_oa_location") or {}
    return {
        "index": "OpenAlex",
        "title": item.get("title") or "",
        "authors": [
            a["author"]["display_name"]
            for a in item.get("authorships", [])
            if (a.get("author") or {}).get("display_name")
        ],
        "doi": doi_of(item.get("doi")),
        "arxiv_id": arxiv,
        "url": f"https://doi.org/{doi_of(item['doi'])}" if item.get("doi") else item.get("id"),
        "index_url": item.get("id"),
        "years": [item["publication_year"]] if item.get("publication_year") else [],
        "venues": names,
        "abstract": " ".join(token for _, token in sequence),
        "open_access_url": oa.get("landing_page_url") or oa.get("pdf_url"),
        "oa_pdf": oa.get("pdf_url") or (item.get("open_access") or {}).get("oa_url") or "",
        "retracted": bool(item.get("is_retracted")),
        "pmid": str(ids.get("pmid") or "").rsplit("/", 1)[-1],
    }


def _semantic(item):
    ids = item.get("externalIds") or {}
    venue = item.get("publicationVenue") or {}
    names = (venue.get("name"), item.get("venue"), (item.get("journal") or {}).get("name"))
    paper = item.get("paperId")
    return {
        "index": "Semantic Scholar",
        "title": item.get("title") or "",
        "authors": [a["name"] for a in item.get("authors") or [] if a.get("name")],
        "doi": doi_of(ids.get("DOI")),
        "arxiv_id": ids.get("ArXiv") or "",
        "url": f"https://www.semanticscholar.org/paper/{paper}" if paper else "",
        "years": [item["year"]] if item.get("year") else [],
        "venues": list(dict.fromkeys(name for name in names if name)),
        "venue_aliases": venue.get("alternate_names") or [],
        "venue_types": [venue["type"]] if venue.get("type") else [],
    }


def _epmc(item):
    pmcid = item.get("pmcid") or ""
    return {
        "index": "Europe PMC",
        "title": item.get("title") or "",
        "doi": doi_of(item.get("doi")),
        "authors": [
            f"{a['lastName']}, {a.get('firstName') or ''}"
            for a in (item.get("authorList") or {}).get("author", [])
            if a.get("lastName")
        ],
        "years": [y] if (y := _year(item.get("pubYear"))) else [],
        "venues": [v]
        if (v := (item.get("journalInfo") or {}).get("journal", {}).get("title"))
        else [],
        "url": f"https://europepmc.org/articles/{pmcid}"
        if pmcid
        else f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id', '')}",
        "abstract": plain(item.get("abstractText")),
        "pmcid": pmcid,
        "retracted": bool(
            re.search(r"Retracted Publication", str(item.get("pubTypeList", {})), re.I)
        ),
    }


def _openreview(note):
    def field(name):
        value = (note.get("content") or {}).get(name)
        return value.get("value") if isinstance(value, dict) else value

    venue = field("venue") or ""
    venueid = field("venueid") or ""
    year = _year(venue) or _year(venueid)
    if not year and note.get("pdate"):
        year = _year(time.strftime("%Y", time.gmtime(note["pdate"] / 1000)))
    ident = note.get("forum") or note.get("id") or ""
    authors = field("authors")
    return {
        "index": "OpenReview",
        "title": plain(field("title") or ""),
        "authors": [a for a in authors if isinstance(a, str)] if isinstance(authors, list) else [],
        "doi": doi_of(field("doi")) if field("doi") else "",
        "url": f"https://openreview.net/forum?id={ident}" if ident else "",
        "years": [year] if year else [],
        "venues": [venue] if venue else ([venueid] if venueid else []),
        "abstract": plain(field("abstract") or ""),
        "retracted": False,
    }


def _pubmed(item):
    ids = {a.get("idtype"): a.get("value") for a in item.get("articleids") or []}
    pmid = str(ids.get("pubmed") or item.get("uid") or "")
    pmcid = ids.get("pmc") or ""
    authors = [a["name"] for a in item.get("authors") or [] if a.get("name")]
    journal = item.get("fulljournalname") or item.get("source") or ""
    return {
        "index": "PubMed",
        "title": plain(item.get("title") or "").rstrip("."),
        "authors": authors,
        "doi": doi_of(ids.get("doi")) if ids.get("doi") else "",
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
        "years": [y] if (y := _year(item.get("pubdate"))) else [],
        "venues": [journal] if journal else [],
        "pmid": pmid,
        "pmcid": pmcid,
        "retracted": bool(
            re.search(r"retract", " ".join(map(str, item.get("pubtype") or [])), re.I)
        ),
    }


def _pubmed_search(title, transport, attempts):
    phrase = " ".join(words(title))
    if not phrase:
        return []
    url = EUTILS + "/esearch.fcgi?" + urlencode(
        {"db": "pubmed", "retmode": "json", "retmax": 5, "term": phrase + "[Title]"}
    )
    status, content = transport.get(url)
    attempts.append({"index": "PubMed", "mode": "search", "http_status": status or None})
    if status != 200:
        return None
    try:
        ids = [str(i) for i in json.loads(content)["esearchresult"]["idlist"]]
    except (ValueError, KeyError, TypeError):
        attempts[-1]["response"] = "invalid"
        return None
    if not ids:
        return []
    url = EUTILS + "/esummary.fcgi?" + urlencode(
        {"db": "pubmed", "retmode": "json", "id": ",".join(ids)}
    )
    status, content = transport.get(url)
    attempts.append({"index": "PubMed", "mode": "summary", "http_status": status or None})
    if status != 200:
        return None
    try:
        result = json.loads(content)["result"]
        return [_pubmed(result[i]) for i in result.get("uids", ids) if isinstance(result.get(i), dict)]
    except (ValueError, KeyError, TypeError, AttributeError):
        attempts[-1]["response"] = "invalid"
        return None


def _atom(content):
    namespace = {"a": "http://www.w3.org/2005/Atom"}
    for item in ET.fromstring(content).findall("a:entry", namespace):
        url = item.findtext("a:id", default="", namespaces=namespace)
        arxiv = arxiv_of(url)
        if not arxiv:
            continue
        yield {
            "index": "arXiv",
            "title": " ".join(item.findtext("a:title", default="", namespaces=namespace).split()),
            "arxiv_id": arxiv,
            "url": "https://arxiv.org/abs/" + arxiv,
            "authors": [v.text for v in item.findall("a:author/a:name", namespace) if v.text],
            "years": [y]
            if (y := _year(item.findtext("a:published", namespaces=namespace)))
            else [],
            "venues": ["arXiv"],
            "abstract": " ".join(
                item.findtext("a:summary", default="", namespaces=namespace).split()
            ),
        }


def prefetch_arxiv(ids, transport, batch=50):
    """Fetch many arXiv records in one API call per batch and store each as if it had
    been fetched alone. arXiv asks for one request every three seconds, so per-reference
    calls serialize a whole run; this turns N waits into ceil(N / batch)."""
    if transport.offline or transport.cache is None:
        return 0
    stored = 0
    todo = [i for i in dict.fromkeys(ids) if i]
    todo = [
        i
        for i in todo
        if not (transport.cache / transport.key(ARXIV + "?" + urlencode({"id_list": i}))).exists()
    ]
    namespace = "http://www.w3.org/2005/Atom"
    ET.register_namespace("", namespace)
    for start in range(0, len(todo), batch):
        group = todo[start : start + batch]
        url = ARXIV + "?" + urlencode({"id_list": ",".join(group), "max_results": len(group)})
        status, content = transport.get(url)
        if status != 200:
            continue
        try:
            entries = ET.fromstring(content).findall(f"{{{namespace}}}entry")
        except ET.ParseError:
            continue
        by_id, by_base = {}, {}
        for entry in entries:
            found = arxiv_of(entry.findtext(f"{{{namespace}}}id", default=""))
            if found:
                by_id.setdefault(found, entry)
                by_base.setdefault(_arxiv_base(found), entry)
        for wanted in group:
            # A version-pinned request must get that version, never another one.
            pinned = re.search(r"v\d+$", wanted)
            entry = by_id.get(wanted) if pinned else by_base.get(wanted)
            if entry is None:
                continue  # left for the single-ID route, which records its own status
            feed = ET.Element(f"{{{namespace}}}feed")
            feed.append(entry)
            transport.store(
                ARXIV + "?" + urlencode({"id_list": wanted}), 200, ET.tostring(feed)
            )
            stored += 1
    return stored


def _semantic_id(ref):
    if ref.get("doi"):
        return "DOI:" + ref["doi"]
    if ref.get("arxiv_id"):
        return "ARXIV:" + _arxiv_base(ref["arxiv_id"])
    return ""


def prefetch_semantic(refs, transport, batch=400):
    """Fetch Semantic Scholar records for every reference with a DOI or arXiv ID in one
    POST per batch. The keyless pool allows about one request per second, so per-reference
    title matching is the slowest step of a run. A prefetched record replaces the title
    match only when its title equals the cited title, so decisions do not change."""
    ids = list(dict.fromkeys(i for i in map(_semantic_id, refs) if i))
    found = 0
    for start in range(0, len(ids), batch):
        group = ids[start : start + batch]
        body = json.dumps({"ids": group}, separators=(",", ":")).encode()
        status, content = transport.get(
            SEMANTIC_BATCH + "?" + urlencode({"fields": SEMANTIC_FIELDS}), data=body
        )
        if status != 200:
            continue
        try:
            items = json.loads(content)
        except ValueError:
            continue
        if not isinstance(items, list) or len(items) != len(group):
            continue
        for key, item in zip(group, items, strict=True):
            if isinstance(item, dict) and item.get("title"):
                transport.prefetched[key] = _semantic(item)
                found += 1
    return found


def _latest_arxiv(pinned, rows, transport, attempts):
    """arXiv answers a versioned id with that version only; ask for the bare id to
    learn the newest version and record it on the matching row."""
    targets = [r for r in rows if r.get("arxiv_id") == pinned and r.get("index") == "arXiv"]
    if not targets:
        return
    url = ARXIV + "?" + urlencode({"id_list": _arxiv_base(pinned)})
    status, content = transport.get(url)
    attempts.append({"index": "arXiv", "mode": "latest_version", "http_status": status or None})
    if status != 200:
        return
    try:
        newest = next(
            (r["arxiv_id"] for r in _atom(content) if _version_number(r["arxiv_id"])), ""
        )
    except ET.ParseError:
        return
    if newest and _arxiv_base(newest) == _arxiv_base(pinned):
        for row in targets:
            row["latest_arxiv"] = newest


def lookup(ref, transport, indices, want_text=False):
    rows, attempts, completed = [], [], []
    doi, arxiv, title = (ref.get(k) for k in ("doi", "arxiv_id", "title"))
    query = title or str(ref.get("raw") or "")[:400]
    routes = []
    if arxiv and "arxiv" in indices:
        routes.append(("arxiv", ARXIV + "?" + urlencode({"id_list": arxiv}), "exact"))
    if doi:
        if "crossref" in indices:
            routes.append(("crossref", CROSSREF + "/" + quote(doi, safe="/"), "exact"))
        if "openalex" in indices:
            routes.append(
                ("openalex", OPENALEX + "/https://doi.org/" + quote(doi, safe="/"), "exact")
            )
        if "datacite" in indices:
            # Software, datasets and many preprint servers register DOIs with DataCite,
            # which Crossref does not hold. Only consulted when Crossref and OpenAlex miss.
            routes.append(("datacite", DATACITE + quote(doi, safe="/"), "exact"))
        if want_text and "europepmc" in indices:
            routes.append(
                (
                    "europepmc",
                    EUROPE
                    + "/search?"
                    + urlencode(
                        {
                            "query": f'DOI:"{doi}"',
                            "format": "json",
                            "resultType": "core",
                            "pageSize": 3,
                        }
                    ),
                    "exact",
                )
            )
    if query:
        if "crossref" in indices:
            routes.append(
                (
                    "crossref",
                    CROSSREF + "?" + urlencode({"query.bibliographic": query, "rows": 5}),
                    "search",
                )
            )
        if "openalex" in indices:
            routes.append(
                ("openalex", OPENALEX + "?" + urlencode({"search": query, "per_page": 5}), "search")
            )
    if title and "semanticscholar" in indices:
        # Runs after an exact match too: it often holds the conference venue and year
        # that preprint records lack.
        params = urlencode({"query": title, "fields": SEMANTIC_FIELDS})
        routes.append(("semanticscholar", SEMANTIC + "?" + params, "venue"))

    if title and "openreview" in indices:
        params = urlencode({"term": title, "limit": 5, "source": "forum"})
        routes.append(("openreview", OPENREVIEW + "?" + params, "search"))
    if title and "pubmed" in indices:
        routes.append(("pubmed", "", "search"))

    for index, url, mode in routes:
        found_exact = any(_scores(ref, row)[0] for row in rows)
        if mode == "search" and found_exact:
            continue
        if mode == "exact" and found_exact and not want_text:
            continue
        if index == "semanticscholar":
            early = transport.prefetched.get(_semantic_id(ref))
            if early and _title(early["title"]) == _title(title):
                rows.append(early)
                completed.append(INDEX_NAMES[index])
                attempts.append(
                    {"index": INDEX_NAMES[index], "mode": mode, "http_status": 200, "batch": True}
                )
                continue
        if index == "pubmed":
            found = _pubmed_search(title, transport, attempts)
            if found is not None:
                rows.extend(found)
            continue
        status, content = transport.get(url)
        attempts.append({"index": INDEX_NAMES[index], "mode": mode, "http_status": status or None})
        if status == 404 and index not in EXTRA_INDICES:
            completed.append(INDEX_NAMES[index])
        if status != 200:
            continue
        try:
            if index == "arxiv":
                found = list(_atom(content))
            else:
                data = json.loads(content)
                if index == "crossref":
                    block = data["message"]
                    found = [
                        _crossref(item)
                        for item in (block["items"] if mode == "search" else [block])
                    ]
                elif index == "openalex":
                    found = [
                        _openalex(item)
                        for item in (data["results"] if mode == "search" else [data])
                    ]
                elif index == "semanticscholar":
                    found = [_semantic(item) for item in data["data"]]
                elif index == "datacite":
                    found = [_datacite(data["data"])]
                elif index == "openreview":
                    found = [_openreview(n) for n in data["notes"] if n.get("content")]
                else:
                    found = [_epmc(item) for item in data["resultList"]["result"]]
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, ET.ParseError):
            attempts[-1]["response"] = "invalid"
            continue
        rows.extend(found)
        if index not in EXTRA_INDICES:
            completed.append(INDEX_NAMES[index])
    if arxiv and _version_number(arxiv) and "arxiv" in indices:
        _latest_arxiv(arxiv, rows, transport, attempts)
    exact = [a["http_status"] for a in attempts if a["mode"] == "exact" and a["index"] != "arXiv"]
    unregistered = False
    if doi and exact and all(status == 404 for status in exact):
        status, _ = transport.get(HANDLES + quote(doi, safe="/"))
        attempts.append({"index": "doi.org", "mode": "registration", "http_status": status or None})
        unregistered = status == 404
    searchable = {"crossref", "openalex", "semanticscholar"} & set(indices)
    quorum = min(2, len(searchable)) or 1
    result = identity(ref, rows, completed, unregistered, min_indices=quorum)
    # Before calling a titled work missing, search arXiv titles. Many conference papers
    # (PMLR, OpenReview) are absent from Crossref but have an arXiv version.
    if result["status"] in ("not_found", "lookup_unavailable") and title and "arxiv" in indices:
        phrase = " ".join(words(title))
        url = ARXIV + "?" + urlencode({"search_query": f'ti:"{phrase}"', "max_results": 5})
        status, content = transport.get(url)
        attempts.append({"index": "arXiv", "mode": "title", "http_status": status or None})
        if status == 200:
            try:
                rows.extend(_atom(content))
                completed.append("arXiv")
            except ET.ParseError:
                attempts[-1]["response"] = "invalid"
            result = identity(ref, rows, completed, unregistered, min_indices=quorum)
    result["attempts"] = attempts
    return result, rows


def _epmc_text(content):
    root = ET.fromstring(content)
    sections = root.findall(".//front//abstract") + root.findall(".//body")
    return "\n\n".join(
        " ".join("".join(node.itertext()).split())
        for section in sections
        for node in section.iter()
        if node.tag in ("title", "p", "caption", "tr") and "".join(node.itertext()).strip()
    )


def _arxiv_text(record, transport):
    arxiv = quote(record["arxiv_id"], safe="/")
    url = "https://arxiv.org/html/" + arxiv
    status, content = transport.get(url)
    if status == 200:
        page = _Page()
        page.feed(content.decode("utf-8", errors="replace"))
        body = "\n\n".join(
            " ".join(part.split()) for part in "".join(page.parts).split("\n\n") if part.strip()
        )
        if (
            len(body) > 1000
            and page.title
            and _similar(_title(page.title), _title(record["title"])) >= 0.9
        ):
            return {"text": body, "level": "full_text", "url": url, "origin": "arXiv HTML"}
    url = "https://arxiv.org/pdf/" + arxiv
    status, content = transport.get(url, limit=30_000_000)
    if status != 200 or not content.startswith(b"%PDF-"):
        return None
    with TemporaryDirectory() as directory:
        path = Path(directory) / "paper.pdf"
        path.write_bytes(content)
        try:
            body = local_text(path)
        except (ValueError, OSError, subprocess.SubprocessError):
            return None
    if references := REF_HEADING.search(body):
        body = body[: references.start()]
    if len(body.strip()) < 1000 or _title(record["title"]) not in _title(body[:8000]):
        return None
    return {"text": body, "level": "full_text", "url": url, "origin": "arXiv PDF"}


def _pdf_text(url, record, transport, origin, web=True):
    try:
        status, content = transport.get(url, limit=30_000_000, web=web)
    except ValueError:
        return None
    if status != 200 or not content.startswith(b"%PDF-"):
        return None
    with TemporaryDirectory() as directory:
        path = Path(directory) / "paper.pdf"
        path.write_bytes(content)
        try:
            body = local_text(path)
        except (ValueError, OSError, subprocess.SubprocessError):
            return None
    if references := REF_HEADING.search(body):
        body = body[: references.start()]
    if len(body.strip()) < 1000 or _title(record["title"]) not in _title(body[:8000]):
        return None
    return {"text": body, "level": "full_text", "url": url, "origin": origin}


def _oa_text(matching, transport):
    """Open-access PDF full text: OpenAlex location, then Unpaywall (only with a
    mailto address) and CORE (only with CORE_API_KEY)."""
    for row in matching:
        if row.get("oa_pdf") and (
            source := _pdf_text(row["oa_pdf"], row, transport, "Open access PDF")
        ):
            return source
    for row in matching:
        doi = row.get("doi")
        if not doi or not row.get("title"):
            continue
        email = getattr(transport, "email", None)
        if email:
            url = UNPAYWALL + quote(doi, safe="/") + "?" + urlencode({"email": email})
            status, content = transport.get(url)
            try:
                location = (json.loads(content).get("best_oa_location") or {}) if status == 200 else {}
            except (ValueError, AttributeError):
                location = {}
            if location.get("url_for_pdf") and (
                source := _pdf_text(location["url_for_pdf"], row, transport, "Open access PDF")
            ):
                return source
        if os.environ.get("CORE_API_KEY"):
            url = CORE + "?" + urlencode({"q": f'doi:"{doi}"', "limit": 1})
            status, content = transport.get(url)
            try:
                results = json.loads(content).get("results") or [] if status == 200 else []
            except (ValueError, AttributeError):
                results = []
            link = results[0].get("downloadUrl") if results and isinstance(results[0], dict) else ""
            if link and (source := _pdf_text(link, row, transport, "Open access PDF")):
                return source
    return None


def get_source(ref, resolution, rows, transport, root, abstract_only=False):
    if "text" in ref or "path" in ref:
        if "text" in ref and "path" in ref:
            raise ValueError(f"{ref['id']}: specify either source text or a path")
        text = ref.get("text") if "text" in ref else local_text(root / ref["path"])
        level = ref.get("level") or "local"
        if level not in ("local", "abstract", "excerpt", "full_text"):
            raise ValueError(f"{ref['id']}: invalid supplied source level")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{ref['id']}: source text is empty")
        return {
            "text": text,
            "level": level,
            "origin": "user_supplied",
            "url": ref.get("url") or "",
        }
    selected = resolution.get("source")
    identity_conflict = any(
        d["field"] in ("doi", "arxiv_id", "title") for d in resolution.get("differences", [])
    )
    if not selected or resolution["status"] == "ambiguous" or identity_conflict:
        return None
    matching = [
        row
        for row in rows
        if _same_record(row, selected) or (row.get("url") and row["url"] == selected.get("url"))
    ]
    requested = ref.get("arxiv_id", "")
    pinned = bool(re.search(r"v\d+$", requested))
    if requested:
        # The cited arXiv record is the same work even when the index chose the
        # published version's record, and arXiv holds its full text.
        matching += [
            r for r in rows
            if r not in matching and r.get("arxiv_id") and _arxiv_base(r["arxiv_id"]) == _arxiv_base(requested)
        ]
    doi = ref.get("doi") or ""
    publication = doi and not doi.startswith("10.48550/arxiv.")
    if publication:
        if pinned:
            # The citation names two versions. Use an explicitly supplied source to resolve it.
            return None
        matching = [r for r in matching if r.get("doi") == doi]
    a = next(
        (
            r
            for r in matching
            if not publication
            and r.get("arxiv_id")
            and (not pinned or _arxiv_base(r["arxiv_id"]) == _arxiv_base(requested))
        ),
        None,
    )
    if pinned:
        a = {**a, "arxiv_id": requested} if a else None
        matching = [r for r in matching if r.get("arxiv_id") == requested]
    if a is not None and not abstract_only and (source := _arxiv_text(a, transport)):
        return source
    if not abstract_only:
        pmc = next((row for row in matching if re.fullmatch(r"PMC\d+", row.get("pmcid", ""))), None)
        if pmc is not None:
            url = EUROPE + "/" + pmc["pmcid"] + "/fullTextXML"
            status, content = transport.get(url)
            if status == 200:
                try:
                    body = _epmc_text(content)
                except ET.ParseError:
                    body = ""
                if len(body) > 1000:
                    return {
                        "text": body,
                        "level": "full_text",
                        "url": pmc["url"],
                        "origin": "Europe PMC",
                    }
    if not abstract_only and (source := _oa_text(matching, transport)):
        return source
    for match in matching:
        if match.get("abstract"):
            return {
                "text": match["abstract"],
                "level": "abstract",
                "url": _location(match),
                "origin": match["index"],
            }
    return None
