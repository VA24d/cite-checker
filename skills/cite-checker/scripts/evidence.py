"""Deterministic passage retrieval and citation evidence validation."""

import hashlib
import json
import math
import re
from collections import Counter

TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "had",
        "has",
        "have",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "with",
    ]
)
VERDICTS = {"supported", "contradicted", "partially_supported", "insufficient"}
DECISIVE = VERDICTS - {"insufficient"}
ABSTRACT_CONTEXT_LIMIT = 8_000


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tokens(text):
    return [t for t in TOKEN.findall(text.casefold()) if t not in STOP]


def _chunks(text, size=850, overlap=160):
    # Adapted from auditor/retrieval/passages.py. Keep raw character offsets and
    # end at a sentence boundary when it leaves enough context.
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            segment = text[start + size // 2 : end]
            cuts = list(re.finditer(r"[.!?]\s|\n{2,}", segment))
            if cuts:
                end = start + size // 2 + cuts[-1].end()
            else:
                cut = text.rfind(" ", start + size // 2, end)
                if cut > start:
                    end = cut
        yield {"start": start, "end": end, "text": text[start:end]}
        if end == len(text):
            break
        start = end - overlap
        boundary = text.find(" ", start, start + 40)
        if boundary != -1:
            start = boundary + 1


def rank(text, query, top=5):
    chunks = list(_chunks(text))
    if not chunks:
        return []
    counts = [Counter(_tokens(c["text"])) for c in chunks]
    lengths = [sum(c.values()) for c in counts]
    avg_len = sum(lengths) / len(chunks) or 1.0
    frequency = Counter(token for chunk in counts for token in chunk)
    terms = dict.fromkeys(_tokens(query))
    n = len(chunks)
    scores = []
    for chunk, length in zip(counts, lengths, strict=True):
        score = 0.0
        for token in terms:
            hits, df = chunk[token], frequency[token]
            if hits:
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                score += idf * hits * 2.5 / (hits + 1.5 * (0.25 + 0.75 * length / avg_len))
        scores.append(score)
    order = sorted(range(n), key=lambda i: (-scores[i], i))
    return [chunks[i] for i in order[:top]]


def _initial_passages(source, query, top):
    if source is None:
        return []
    text = source["text"]
    if source.get("level") == "abstract" and len(text) <= ABSTRACT_CONTEXT_LIMIT:
        return [{"start": 0, "end": len(text), "text": text}]
    return rank(text, query, top)


def packet(refs, claims, resolutions, bodies, notices, top=5):
    sources = {}
    for key, body in bodies.items():
        if body:
            sources[key] = {**body, "sha256": _digest(body["text"])}
    pairs = []
    for claim in claims:
        for key in dict.fromkeys(claim["references"]):
            source = sources.get(key)
            binding = json.dumps(
                [claim["id"], claim["text"], key, source["sha256"] if source else None],
                ensure_ascii=False,
            )
            pair_id = f"pair-{len(pairs) + 1:04d}-{_digest(binding)[:16]}"
            pairs.append(
                {
                    "id": pair_id,
                    "claim_id": claim["id"],
                    "claim": claim["text"],
                    "reference_id": key,
                    "source": {k: v for k, v in source.items() if k != "text"} if source else None,
                    "passages": _initial_passages(source, claim["text"], top),
                    **{k: claim[k] for k in ("sentence", "first_person") if claim.get(k)},
                }
            )
    return {
        "version": 1,
        "coverage_notes": notices,
        "references": [
            resolutions.get(r["id"], {"id": r["id"], "status": "not_assessed"}) for r in refs
        ],
        "pairs": pairs,
        "sources": sources,
    }


def _checked_sources(run):
    if run.get("version") != 1:
        raise ValueError("unsupported packet version")
    for ref_id, source in run["sources"].items():
        if _digest(source["text"]) != source["sha256"]:
            raise ValueError(f"Source digest changed for {ref_id}. Rebuild the packet.")
    for pair in run["pairs"]:
        source = run["sources"].get(pair["reference_id"])
        expected = {k: v for k, v in source.items() if k != "text"} if source else None
        if pair["source"] != expected:
            raise ValueError(
                f"Source binding or digest changed for {pair['id']}. Rebuild the packet."
            )


def search(run, pair_id, query, top):
    _checked_sources(run)
    pair = next((p for p in run["pairs"] if p["id"] == pair_id), None)
    if pair is None:
        raise ValueError(f"Unknown pair {pair_id}")
    source = run["sources"].get(pair["reference_id"])
    return rank(source["text"], query, top) if source else []


def _locate(proposal, source, ref_id):
    if isinstance(proposal, str):
        quote, start = proposal, None
    elif isinstance(proposal, dict):
        quote, start = proposal.get("text"), proposal.get("start")
        if proposal.get("reference_id", ref_id) != ref_id:
            return None, "quote is from a different reference"
        if proposal.get("sha256", source["sha256"]) != source["sha256"]:
            return None, "quote names a different source digest"
    else:
        return None, "invalid quote object"
    if not isinstance(quote, str) or len(quote.strip()) < 12 or len(quote) > 2500:
        return None, "quote must contain 12 to 2500 characters"
    text = source["text"]
    if start is None:
        start = text.find(quote)
        if start < 0:
            return None, "quote is absent from the cited source snapshot"
        if text.find(quote, start + 1) >= 0:
            return None, "quote repeats in the source; provide the selected occurrence's offset"
    elif type(start) is not int or start < 0 or not text.startswith(quote, start):
        return None, "quote does not match the supplied offset in the cited source snapshot"
    end = start + len(quote)
    return {
        "reference_id": ref_id,
        "quote": text[start:end],
        "start": start,
        "end": end,
        "sha256": source["sha256"],
        "url": source.get("url", ""),
        "level": source["level"],
        "origin": source.get("origin", "unspecified"),
    }, None


# Signs that a passage reports someone else's finding rather than the source's own.
ATTRIBUTION = re.compile(
    r"\[\d+(?:\s*[,\u2013-]\s*\d+)*\]"
    r"|\((?:[A-Z][\w'-]+(?:\s+(?:et al\.?|and|&)\s*[A-Z]?[\w'-]*)?,?\s+(?:19|20)\d{2}[a-z]?(?:;\s*)?)+\)"
    r"|\b[A-Z][\w'-]+\s+et al\.?\s*\(?(?:19|20)\d{2}"
    r"|\b(?:previous|prior|earlier|other) (?:work|studies|research|reports)\b"
    r"|\bhas been (?:shown|reported|demonstrated|found)\b"
    r"|\b(?:reported|shown|found|proposed|observed) (?:by|in) [A-Z]"
)


def _attribution(text, start, end):
    """Return the citation-like marker in the sentence around a quote, if any."""
    left = max(text.rfind(". ", max(0, start - 300), start), text.rfind("\n", max(0, start - 300), start))
    left = max(left + 1, start - 300, 0)
    stops = [i for i in (text.find(". ", end), text.find("\n", end)) if i >= 0]
    right = min([*stops, end + 300]) + 1
    match = ATTRIBUTION.search(text[left:right])
    return match[0] if match else ""


def gate(run, proposals):
    _checked_sources(run)
    ids = {p["id"] for p in run["pairs"]}
    by_id = {}
    for row in proposals:
        key = row["pair_id"]
        if key not in ids or key in by_id:
            raise ValueError(f"Unknown or duplicate pair id: {key}")
        if row.get("verdict") not in VERDICTS:
            raise ValueError(f"{key}: expected one of {', '.join(sorted(VERDICTS))}")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError(f"{key}: a reason is required")
        if not isinstance(row.get("quotes", []), list) or len(row.get("quotes", [])) > 8:
            raise ValueError(f"{key}: quotes must be a list of at most eight items")
        by_id[key] = row
    claims = []
    for pair in run["pairs"]:
        key, ref_id = pair["id"], pair["reference_id"]
        source, proposal = run["sources"].get(ref_id), by_id.get(key)
        issues, evidence = [], []
        if source is None:
            verdict, reason = "insufficient", "No readable cited source was captured."
            if proposal and proposal["verdict"] in DECISIVE:
                issues.append("A definitive assessment requires source text.")
            elif proposal:
                reason = f"{reason} {proposal['reason'].strip()}"
        elif proposal is None:
            verdict, reason = "not_assessed", "The host agent did not submit an assessment."
        else:
            verdict, reason = proposal["verdict"], proposal["reason"].strip()
            for quote in proposal.get("quotes", []):
                match, error = _locate(quote, source, ref_id)
                if error:
                    issues.append(error)
                elif match not in evidence:
                    evidence.append(match)
            if verdict in DECISIVE and (not evidence or issues):
                if not evidence:
                    issues.append("A definitive assessment requires an exact quote.")
                verdict = "insufficient"
                reason = "The proposed assessment did not pass quote validation."
        secondary = ""
        if verdict in ("supported", "partially_supported") and evidence:
            marks = [_attribution(source["text"], e["start"], e["end"]) for e in evidence]
            if all(marks):
                secondary = marks[0]
        claims.append(
            {
                "pair_id": key,
                "claim_id": pair["claim_id"],
                "claim": pair["claim"],
                "reference_id": ref_id,
                "verdict": verdict,
                "reason": reason,
                "proposed_verdict": proposal["verdict"] if proposal else None,
                "caveat": str((proposal or {}).get("caveat") or "").strip(),
                "split": bool((proposal or {}).get("split")),
                "secondary": secondary,
                **({"candidate": pair["candidate"], "match": pair.get("match")} if pair.get("candidate") else {}),
                "source": pair["source"],
                "evidence": evidence,
                "gate_issues": issues,
            }
        )
    return {
        "version": 1,
        "coverage_notes": run["coverage_notes"],
        "references": run["references"],
        "claims": claims,
        "structure": run.get("structure", []),
        "manuscript_checks": run.get("manuscript_checks", []),
        "suggested_references": run.get("suggested_references", []),
        "claim_judgment": "host_agent",
    }
