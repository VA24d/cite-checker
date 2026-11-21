"""Find a better citation for claims their cited source does not support.

After `finalize`, every claim that no cited source supports gets candidate sources:

- other references in the same bibliography whose text matches the claim better than the
  cited one does (a likely wrong or swapped citation), and
- with `external=True`, works found by an OpenAlex and Semantic Scholar keyword search.
  Only keywords from the claim are sent, and only when the user allows it.

Candidates become extra claim-source pairs marked `candidate`, written as
`shards/suggest-NN.json` for the same cheap judges. Finalize reports the ones judged
supported as suggested citations and keeps them out of the claim-support counts.
"""

import json
import math
import re
from collections import Counter
from urllib.parse import urlencode

from scripts.evidence import _chunks, _digest, _initial_passages, _tokens
from scripts.sources import OPENALEX, SEMANTIC, _openalex, _semantic

UNSUPPORTED = ("insufficient", "partially_supported", "contradicted")
SEMANTIC_SEARCH = SEMANTIC.rsplit("/", 1)[0]  # .../paper/search
MIN_COVERAGE = 0.45  # share of the claim's weighted terms a candidate passage must hold
MARGIN = 0.10  # how much better than the cited source an internal candidate must match
MIN_QUERY_TERMS = 5  # content words a claim needs before it is searched externally


def _weights(texts):
    """Inverse document frequency over passage chunks of every source."""
    frequency, total = Counter(), 0
    for text in texts:
        for chunk in _chunks(text):
            frequency.update(set(_tokens(chunk["text"])))
            total += 1
    return {t: math.log(1 + total / (1 + n)) for t, n in frequency.items()}, math.log(1 + total)


def _coverage(claim, text, weights, default):
    """Best share of the claim's weighted terms found in one chunk of `text`."""
    terms = {t for t in _tokens(claim) if not t.isdigit()}
    if not terms or not text:
        return 0.0
    total = sum(weights.get(t, default) for t in terms)
    best = 0.0
    for chunk in _chunks(text):
        present = terms & set(_tokens(chunk["text"]))
        best = max(best, sum(weights.get(t, default) for t in present) / total)
    return best


def _keywords(claim, weights, default, limit=8):
    terms = [t for t in dict.fromkeys(_tokens(claim)) if not t.isdigit() and len(t) > 2]
    terms.sort(key=lambda t: -weights.get(t, default))
    chosen = set(terms[:limit])
    return " ".join(t for t in dict.fromkeys(_tokens(claim)) if t in chosen)


def _search(query, transport, per_query):
    """Works matching `query` from OpenAlex and Semantic Scholar, with abstracts."""
    rows = []
    url = OPENALEX + "?" + urlencode(
        {"search": query, "filter": "has_abstract:true", "per-page": per_query}
    )
    try:
        status, body = transport.get(url)
        if status == 200:
            rows += [_openalex(item) for item in json.loads(body).get("results", [])]
    except (OSError, ValueError):
        pass
    fields = "title,abstract,year,authors,venue,externalIds"
    url = SEMANTIC_SEARCH + "?" + urlencode({"query": query, "limit": per_query, "fields": fields})
    try:
        status, body = transport.get(url)
        if status == 200:
            for item in json.loads(body).get("data") or []:
                row = _semantic(item)
                row["abstract"] = row.get("abstract") or item.get("abstract") or ""
                rows.append(row)
    except (OSError, ValueError):
        pass
    seen, unique = set(), []
    for row in rows:
        key = (row.get("doi") or "").lower() or re.sub(r"\W+", "", (row.get("title") or "").lower())
        if row.get("abstract") and key and key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def _norm(title):
    return re.sub(r"[^a-z0-9]+", "", str(title or "").lower())


def _suggested_id(row):
    ident = row.get("doi") or row.get("arxiv_id") or row.get("title") or ""
    return "suggest:" + _digest(ident.lower())[:12]


def suggest(packet, sources, report, transport=None, external=False, per_claim=3, top=5):
    """Add candidate pairs to `packet` and `sources` in place. Returns the new pair ids."""
    claims = {}
    for row in report["claims"]:
        if row.get("candidate"):
            continue
        claims.setdefault(row["claim_id"], []).append(row)
    pairs = {p["claim_id"]: p for p in packet["pairs"] if not p.get("candidate")}
    weights, default = _weights(s["text"] for s in sources.values())
    existing = {(p["claim_id"], p["reference_id"]) for p in packet["pairs"]}
    suggested = {r["id"]: r for r in packet.get("suggested_references", [])}
    known = set()
    for ref in packet["references"]:
        for data in (ref.get("source") or {}, ref.get("reference") or {}):
            known.add(_norm(data.get("title")))
            known.add((data.get("doi") or "").lower())
    known.discard("")
    added = []
    for claim_id, rows in claims.items():
        if any(r["verdict"] == "supported" for r in rows) or not any(
            r["verdict"] in UNSUPPORTED for r in rows
        ):
            continue
        claim = rows[0]["claim"]
        cited = {r["reference_id"] for r in rows}
        floor = max(
            [_coverage(claim, sources[k]["text"], weights, default) for k in cited if k in sources]
            or [0.0]
        )
        scored = []
        for key, source in sources.items():
            if key in cited or key.startswith("suggest:"):
                continue
            score = _coverage(claim, source["text"], weights, default)
            if score >= MIN_COVERAGE and score >= floor + MARGIN:
                scored.append((score, key, "bibliography"))
        terms = {t for t in _tokens(claim) if not t.isdigit() and len(t) > 2}
        # Short claims ("Copilot gave pako@2.1.0") make vague queries; skip the search.
        if external and transport is not None and len(terms) >= MIN_QUERY_TERMS:
            for row in _search(_keywords(claim, weights, default), transport, per_query=5):
                if _norm(row.get("title")) in known or (row.get("doi") or "").lower() in known:
                    continue  # already in the bibliography, so scored above if useful
                if len(terms & set(_tokens(row["abstract"]))) < MIN_QUERY_TERMS - 1:
                    continue
                key = _suggested_id(row)
                score = _coverage(claim, row["abstract"], weights, default)
                if score < MIN_COVERAGE:
                    continue
                if key not in sources:
                    sources[key] = {
                        "text": row["abstract"],
                        "level": "abstract",
                        "origin": row.get("index", "search"),
                        "url": row.get("url") or (f"https://doi.org/{row['doi']}" if row.get("doi") else ""),
                        "sha256": _digest(row["abstract"]),
                    }
                    suggested[key] = {
                        "id": key,
                        "status": "suggested",
                        "source": {k: row.get(k) for k in ("title", "authors", "years", "doi", "arxiv_id", "venue", "url")},
                    }
                scored.append((score, key, "search"))
        scored.sort(key=lambda s: (-s[0], s[1]))
        base = pairs.get(claim_id) or {}
        for score, key, origin in scored[:per_claim]:
            if (claim_id, key) in existing:
                continue
            source = sources[key]
            binding = json.dumps([claim_id, claim, key, source["sha256"], "candidate"], ensure_ascii=False)
            pair = {
                "id": f"pair-{len(packet['pairs']) + 1:04d}-{_digest(binding)[:16]}",
                "claim_id": claim_id,
                "claim": claim,
                "reference_id": key,
                "source": {k: v for k, v in source.items() if k != "text"},
                "passages": _initial_passages(source, claim, top),
                "candidate": origin,
                "match": round(score, 2),
                **{k: base[k] for k in ("sentence", "first_person") if base.get(k)},
            }
            packet["pairs"].append(pair)
            existing.add((claim_id, key))
            added.append(pair["id"])
    packet["suggested_references"] = list(suggested.values())
    return added
