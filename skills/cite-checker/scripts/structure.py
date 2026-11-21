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
