"""Deterministic checks of software packages and versions named in claims.

mentions(text) finds package mentions, check(mention, transport) looks one up in
its registry (npm, PyPI, crates.io, GitHub), check_claims() runs both over claims.
"""

import json
import re
from urllib.parse import quote

FULL_LIMIT = 20_000_000  # the full npm document for large packages is >10 MB

_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_VER = r"v?\d+\.\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.+-]*[0-9A-Za-z])?"
_STOP = {
    "a", "an", "the", "that", "this", "these", "those", "called", "named", "and", "or", "of",
    "for", "is", "are", "was", "which", "in", "on", "with", "package", "packages", "module",
    "library", "crate", "as", "to", "it", "exist", "exists", "existed", "published",
    "available", "found", "listed", "hosted", "installed", "them", "up", "not", "only",
}

_NPM_AT = re.compile(
    rf"(?<![\w@/.+-])(?P<name>(?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*)@(?P<ver>{_VER})(?![\w@-])"
)
_PIP_EQ = re.compile(rf"(?<![\w.-])(?P<name>[A-Za-z][\w.-]*)==(?P<ver>\d[\w.!+*-]*[\w*])")
_PIP_INSTALL = re.compile(
    rf"\bpip3?\s+install\s+(?:-[-\w]+(?:[= ]\S+)?\s+)*(?P<name>[A-Za-z][\w.-]*)(?:==(?P<ver>\d[\w.]*\w))?"
)
_CARGO_ADD = re.compile(rf"\bcargo\s+(?:add|install)\s+(?P<name>[A-Za-z][\w-]*)(?:@(?P<ver>{_VER}))?")
_GITHUB = re.compile(r"github\.com/(?P<owner>[A-Za-z0-9][\w-]*)/(?P<repo>[\w.-]+)")
_PHRASE = re.compile(
    rf"\b(?P<eco>npm|PyPI|pip|crates\.io|cargo)\s+(?:package|module|library|crate)s?\s+"
    rf"(?:(?:called|named)\s+)?[`'\"]?(?P<name>@?[\w.-]+(?:/[\w.-]+)?)[`'\"]?"
    rf"(?:(?:@|\s+v?)(?P<ver>\d+\.\d+(?:\.\d+)*))?",
    re.I,
)
_ON_REG = re.compile(
    r"[`'\"](?P<name>@?[\w.-]+(?:/[\w.-]+)?)[`'\"]\s+(?:package\s+|crate\s+)?"
    r"(?:on|in|from)\s+(?:the\s+)?(?P<eco>npm|PyPI|crates\.io)\b"
    r"|\b(?P<name2>@?[a-z][\w.-]*(?:/[\w.-]+)?)\s+(?:package\s+|crate\s+)?on\s+(?P<eco2>npm|PyPI|crates\.io)\b",
)
_INSTEAD = re.compile(
    r"\bsuch as\s+[`'\"]?(?P<name>@?[\w.-]+(?:/[\w.-]+)?)[`'\"]?\s+(?:instead of|rather than)\s+"
    r"[`'\"]?(?P<real>@?[\w.-]+(?:/[\w.-]+)?)[`'\"]?",
    re.I,
)
_MISSING = re.compile(
    r"(?:do(?:es)?\s+not|don'?t|doesn'?t|did\s+not|didn'?t)\s+exist"
    r"|non-?existent|no such (?:package|crate|module|library|version)"
    r"|(?:is|are|was|were)\s+not\s+(?:published|available|found|present|registered|listed)"
    r"|isn'?t\s+(?:published|available|on|in)|not\s+(?:on|in)\s+(?:the\s+)?(?:npm|PyPI|registry|crates)"
    r"|hallucinat|fabricat|phantom|made[- ]up|invented\s+(?:package|name)|never\s+(?:existed|published)"
    r"|does not exist|404",
    re.I,
)
_PIP_CTX = re.compile(r"\b(?:pip\d?|pypi|python|conda)\b", re.I)
_CARGO_CTX = re.compile(r"\b(?:cargo|crates\.io|rust)\b", re.I)
_REGISTRY_CTX = re.compile(r"\b(?:npm|pypi|crates\.io|pip|cargo)\b", re.I)
_ECO = {"npm": "npm", "pypi": "pypi", "pip": "pypi", "crates.io": "crates", "cargo": "crates"}


def _sentence_bounds(text, pos):
    start = 0
    for m in re.finditer(r"[.!?;]\s+(?=[A-Z\"'`(\[])|\n\s*\n", text):
        if m.end() <= pos:
            start = m.end()
        else:
            return start, m.start() + 1
    return start, len(text)


def _clean(name):
    return name.rstrip(".,;:)").strip("`'\"")


def mentions(text):
    """Return package mentions in one claim string (precision over recall)."""
    if not text:
        return []
    found = []

    def add(eco, name, ver, span, missing=None):
        name = _clean(name)
        if not name or name.lower() in _STOP or len(name) < 2:
            return
        if ver:
            ver = ver.lstrip("v")
        found.append({"ecosystem": eco, "name": name, "version": ver or None,
                      "span": [span[0], span[1]], "_missing": missing})

    for m in _GITHUB.finditer(text):
        repo = re.sub(r"\.git$", "", m["repo"].rstrip(".,;:)"))
        if repo:
            add("github", f"{m['owner']}/{repo}", None, m.span())
    for m in _PIP_EQ.finditer(text):
        add("pypi", m["name"], m["ver"], m.span())
    for m in _PIP_INSTALL.finditer(text):
        add("pypi", m["name"], m["ver"], m.span())
    for m in _CARGO_ADD.finditer(text):
        add("crates", m["name"], m["ver"], m.span())
    for m in _NPM_AT.finditer(text):
        name = m["name"]
        if "@" not in name and "/" not in name and not re.search(r"[a-z]", name):
            continue
        s, e = _sentence_bounds(text, m.start())
        sent = text[s:e]
        eco = "pypi" if _PIP_CTX.search(sent) and not re.search(r"\bnpm\b", sent, re.I) else "npm"
        if eco == "pypi" and name.startswith("@"):
            eco = "npm"
        elif eco == "npm" and name.startswith("@") is False and _CARGO_CTX.search(sent) \
                and not re.search(r"\bnpm\b", sent, re.I) and re.search(r"\bcrates?\b", sent, re.I):
            eco = "crates"
        add(eco, name, m["ver"], m.span())
    for m in _INSTEAD.finditer(text):
        s, e = _sentence_bounds(text, m.start())
        sent = text[s:e]
        reg = _REGISTRY_CTX.search(sent)
        if reg and not m["name"].lower() in _STOP:
            eco = _ECO[reg.group(0).lower()]
            add(eco, m["name"], None, m.span("name"), missing=True)
            add(eco, m["real"], None, m.span("real"), missing=False)
    for m in _PHRASE.finditer(text):
        add(_ECO[m["eco"].lower()], m["name"], m["ver"], m.span("name"))
    for m in _ON_REG.finditer(text):
        name = m["name"] or m["name2"]
        eco = m["eco"] or m["eco2"]
        add(_ECO[eco.lower()], name, None, m.span("name") if m["name"] else m.span("name2"))

    # Drop overlaps: earlier-added (more specific) patterns win over later ones.
    kept = []
    for c in found:
        if any(c["span"][0] < k["span"][1] and k["span"][0] < c["span"][1] for k in kept):
            continue
        kept.append(c)
    kept.sort(key=lambda c: c["span"][0])

    out = []
    for c in kept:
        forced = c.pop("_missing")
        if forced is None:
            s, e = _sentence_bounds(text, c["span"][0])
            sent = text[s:e]
            before = sent[: c["span"][0] - s]
            forced = bool(_MISSING.search(sent)) and not re.search(
                r"\b(?:instead of|rather than)\b", before, re.I
            )
        c["asserts_missing"] = forced
        out.append(c)
    return out


def _urls(eco, name):
    if eco == "npm":
        return f"https://registry.npmjs.org/{quote(name, safe='@')}", f"https://www.npmjs.com/package/{name}"
    if eco == "pypi":
        return f"https://pypi.org/pypi/{quote(name, safe='')}/json", f"https://pypi.org/project/{name}/"
    if eco == "crates":
        return f"https://crates.io/api/v1/crates/{quote(name, safe='')}", f"https://crates.io/crates/{name}"
    return f"https://api.github.com/repos/{name}", f"https://github.com/{name}"


def _result(status, url, latest=None, published=None, conflict=False):
    return {"status": status, "latest": latest, "published": published, "url": url,
            "conflict": conflict}


def check(mention, transport):
    """Look one mention up in its registry."""
    eco, name = mention["ecosystem"], mention["name"]
    ver = (mention.get("version") or "").lstrip("v") or None
    asserts = bool(mention.get("asserts_missing"))
    api, page = _urls(eco, name)
    try:
        status, body = transport.get(api, limit=FULL_LIMIT, web=True)
    except Exception:
        return _result("lookup_unavailable", page)
    if status == 404:
        return _result("missing", page, conflict=not asserts)
    if status != 200:
        return _result("lookup_unavailable", page)
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return _result("lookup_unavailable", page)
    if not isinstance(doc, dict):
        return _result("lookup_unavailable", page)

    latest = published = None
    versions = None
    if eco == "npm":
        versions = set(doc.get("versions") or {})
        latest = (doc.get("dist-tags") or {}).get("latest")
        published = (doc.get("time") or {}).get(ver) if ver else None
    elif eco == "pypi":
        releases = doc.get("releases") or {}
        versions = set(releases)
        latest = (doc.get("info") or {}).get("version")
        files = releases.get(ver) or []
        if files:
            published = files[0].get("upload_time_iso_8601") or files[0].get("upload_time")
    elif eco == "crates":
        crate = doc.get("crate") or {}
        latest = crate.get("max_stable_version") or crate.get("max_version")
        vlist = doc.get("versions") or []
        versions = {v.get("num") for v in vlist}
        if ver:
            published = next((v.get("created_at") for v in vlist if v.get("num") == ver), None)
    if ver and versions is not None:
        if ver in versions:
            return _result("version_exists", page, latest, published, asserts)
        return _result("version_missing", page, latest, None, not asserts)
    return _result("exists", page, latest, published, asserts)


def check_claims(claims, transport):
    """Run mentions+check over [{"id", "text", ...}], deduping registry calls."""
    cache = {}
    findings = []
    for claim in claims:
        for m in mentions(claim.get("text") or ""):
            key = (m["ecosystem"], m["name"], m["version"], m["asserts_missing"])
            if key not in cache:
                cache[key] = check(m, transport)
            findings.append({"id": claim.get("id"), **m, **cache[key]})
    return findings
