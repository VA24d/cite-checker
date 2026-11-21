"""Deterministic checks of how a bibliography fits the paper and its own style."""

import re
from collections import defaultdict

from scripts.sources import _arxiv_base, _title, _venue

# Fields a reader needs to find each kind of work, in normalized reference terms.
REQUIRED = {
    "article": ("authors", "title", "year", "venue"),
    "inproceedings": ("authors", "title", "year", "venue"),
    "conference": ("authors", "title", "year", "venue"),
    "incollection": ("authors", "title", "year", "venue"),
    "book": ("title", "year"),
    "phdthesis": ("authors", "title", "year"),
    "mastersthesis": ("authors", "title", "year"),
    "techreport": ("authors", "title", "year"),
    "misc": ("title",),
    "online": ("title", "url"),
    "software": ("title", "url"),
    "dataset": ("title",),
}
WEB_TYPES = {"misc", "online", "electronic", "www", "webpage", "software", "dataset"}


def _issue(check, severity, refs, message):
    return {"check": check, "severity": severity, "references": sorted(refs), "message": message}


def _undefined(ref):
    return not any(ref.get(k) for k in ("title", "doi", "arxiv_id", "raw", "url", "text", "path"))


def check(refs, claims, resolutions=None, manuscript=False):
    """Return a list of structure and consistency issues. Pure function of its inputs."""
    resolutions = resolutions or {}
    issues = []
    cited = {key for claim in claims for key in claim["references"]}
    undefined = [r["id"] for r in refs if _undefined(r)]
    if undefined:
        issues.append(
            _issue("undefined_citation", "error", undefined, "Cited key has no bibliography entry.")
        )
    if manuscript and claims:
        uncited = [r["id"] for r in refs if r["id"] not in cited and not _undefined(r)]
        if uncited:
            issues.append(
                _issue(
                    "uncited_entry",
                    "warning",
                    uncited,
                    "Entry is never cited in the text (fine only with \\nocite).",
                )
            )
    groups = defaultdict(set)
    for ref in refs:
        if ref.get("doi"):
            groups["doi:" + ref["doi"]].add(ref["id"])
        if ref.get("arxiv_id"):
            groups["arxiv:" + _arxiv_base(ref["arxiv_id"])].add(ref["id"])
        if len(_title(ref.get("title", "")).split()) >= 4:
            groups["title:" + _title(ref["title"])].add(ref["id"])
    seen = set()
    for keys in groups.values():
        frozen = frozenset(keys)
        if len(keys) > 1 and frozen not in seen:
            seen.add(frozen)
            issues.append(
                _issue("duplicate_entry", "error", keys, "Several keys name the same work.")
            )
    for ref in refs:
        kind = ref.get("entry_type")
        if not kind or _undefined(ref):
            continue
        need = REQUIRED.get(kind, ())
        missing = [f for f in need if not ref.get(f)]
        if kind in ("misc", "online") and not any(ref.get(k) for k in ("url", "doi", "arxiv_id")):
            missing.append("url or identifier")
        if missing:
            issues.append(
                _issue(
                    "incomplete_entry",
                    "warning",
                    [ref["id"]],
                    f"@{kind} entry lacks: {', '.join(missing)}.",
                )
            )
        if (kind in WEB_TYPES or (ref.get("url") and not ref.get("doi"))) and ref.get("url"):
            if not ref.get("accessed"):
                issues.append(
                    _issue(
                        "missing_access_date",
                        "info",
                        [ref["id"]],
                        "Web citation has no access date (urldate); APA, IEEE and Chicago ask for one.",
                    )
                )
    # Entries whose record has a DOI the entry omits, when most entries do carry DOIs.
    with_doi = sum(bool(r.get("doi")) for r in refs if not _undefined(r))
    total = sum(not _undefined(r) for r in refs)
    if total and with_doi / total >= 0.5:
        lacking = [
            r["id"]
            for r in refs
            if not r.get("doi")
            and ((resolutions.get(r["id"]) or {}).get("source") or {}).get("doi")
            and resolutions[r["id"]].get("status") in ("verified", "metadata_unconfirmed")
        ]
        if lacking:
            issues.append(
                _issue(
                    "missing_doi",
                    "info",
                    lacking,
                    "Most entries give a DOI; these have a known DOI but omit it.",
                )
            )
    # One venue written several ways (for example NeurIPS and its full name).
    spellings = defaultdict(set)
    owners = defaultdict(set)
    for ref in refs:
        if ref.get("venue"):
            label = _venue(ref["venue"])
            if label and "arxiv" not in label:
                spellings[label].add(re.sub(r"\s+", " ", ref["venue"].strip()))
                owners[label].add(ref["id"])
    for label, forms in sorted(spellings.items()):
        if len(forms) > 1:
            issues.append(
                _issue(
                    "inconsistent_venue",
                    "info",
                    owners[label],
                    "One venue is written several ways: " + "; ".join(sorted(forms)) + ".",
                )
            )
    # Numbered styles such as IEEE and Vancouver number references by first citation.
    order = list(dict.fromkeys(key for claim in claims for key in claim["references"]))
    numeric = [int(k) for k in order if k.isdigit()]
    if len(numeric) >= 3 and len(numeric) == len(order) and numeric != sorted(numeric):
        issues.append(
            _issue(
                "citation_order",
                "info",
                [],
                "Numbered references are not in order of first citation, as IEEE and "
                "Vancouver styles require.",
            )
        )
    return issues


# A year used as a date in a claim: "in 2019", "since 2021", "March 2026", "as of 2024".
CLAIM_YEAR = re.compile(
    r"\b(?:in|during|since|from|after|until|by|as of|throughout|early|mid|late|"
    r"January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+((?:19|20)\d{2})\b",
    re.I,
)


def _published(ref, resolution):
    years = list(((resolution or {}).get("source") or {}).get("years") or [])
    if ref.get("year"):
        years.append(ref["year"])
    return max(years) if years else None


def date_checks(refs, claims, resolutions, today):
    """Dates a reader can check without judgment: a claim about events after the cited
    work appeared, and access dates in the future or before the page existed."""
    issues = []
    by_id = {r["id"]: r for r in refs}
    for claim in claims:
        years = {int(m[1]) for m in CLAIM_YEAR.finditer(claim["text"]) if not m[0].lower().startswith("by ")}
        for key in claim["references"]:
            published = _published(by_id.get(key, {}), resolutions.get(key))
            late = sorted(y for y in years if published and y > published)
            if late:
                issues.append(
                    {
                        "check": "claim_after_source",
                        "severity": "warning",
                        "references": [key],
                        "claims": [claim["id"]],
                        "message": f"The claim describes {late[0]}, but {key} appeared in {published}. "
                        "A source cannot report later events; check the citation.",
                    }
                )
    for ref in refs:
        accessed = str(ref.get("accessed") or "")
        match = re.match(r"((?:19|20)\d{2})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", accessed)
        if not match:
            continue
        stamp = "-".join(f"{int(x):02d}" if i else x for i, x in enumerate(match.groups()) if x)
        if stamp > today[: len(stamp)]:
            issues.append(
                {"check": "access_in_future", "severity": "error", "references": [ref["id"]], "claims": [],
                 "message": f"Access date {accessed} is in the future."}
            )
        published = _published(ref, resolutions.get(ref["id"]))
        if published and int(match[1]) < published:
            issues.append(
                {"check": "access_before_publication", "severity": "warning", "references": [ref["id"]],
                 "claims": [], "message": f"Access date {accessed} is before the work's date ({published})."}
            )
    return issues
