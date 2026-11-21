"""Benchmark reference checking against the pinned local HALLMARK fixture.

Run from the repository root with Python 3.11 or later:
    python -B cite-checker/benchmarks/hallmark.py --historical runs/hallmark/results_fixed.jsonl

The initial, holdout, confirmation, and final samples take consecutive disjoint
blocks of SHA-256 ranks per label and difficulty.

Strict flags inconsistent metadata and an index miss. Triage sends every
non-verified result for review, including unavailable lookups. The DOI-only
baseline flags a Crossref 404 on a supplied DOI. The confusion counts use all
sampled rows, including cases for which a policy produced no flag.

Use --offline with the same --cache directory to replay successful responses
and explicit misses. The default cache lives in the system temporary directory.
This sample measures reference metadata only, on the pinned local split.
"""

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import gettempdir
from urllib.parse import quote

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "cite-checker"
sys.path.insert(0, str(SKILL_DIR))
from scripts.bibliography import reference  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

from scripts.sources import (  # noqa: E402
    CROSSREF,
    Transport,
    classify,
    lookup,
    prefetch_semantic,
)

FIXTURE_SHA256 = "e42ef2cbe2b5550768c191f0526d9c75935adda5fd630dba8724e479975907d1"
SEED = "cite-checker-hallmark-v1"
# fresh1 and fresh2 were added for version 2 and then used to fix author and quorum rules;
# fresh3 and fresh4 were scored once, after every change.
SPLITS = ("initial", "holdout", "confirmation", "final", "fresh1", "fresh2", "fresh3", "fresh4")
SAMPLE_SIZE = {
    ("VALID", None): 16,
    ("HALLUCINATED", 1): 10,
    ("HALLUCINATED", 2): 10,
    ("HALLUCINATED", 3): 10,
}
INDICES = ("Crossref", "OpenAlex", "Semantic Scholar", "doi.org")
STRICT_FLAGS = {"metadata_mismatch", "not_found"}
HISTORICAL_FLAGS = {"reference_metadata_mismatch", "reference_not_found"}


def sample(rows, split):
    strata = defaultdict(list)
    labels = {}
    for row in rows:
        key = row["bibtex_key"]
        stratum = (row["label"], row.get("difficulty_tier"))
        strata[stratum].append(key)
        labels[key] = (*stratum, row.get("hallucination_type"))
    selected = []
    for stratum, count in SAMPLE_SIZE.items():
        ranked = sorted(
            strata[stratum], key=lambda key: hashlib.sha256(f"{SEED}:{key}".encode()).digest()
        )
        start = SPLITS.index(split) * count
        selected.extend(ranked[start : start + count])
    return sorted(selected), labels


def doi_status(ref, transport):
    if not ref["doi"]:
        return "no_doi"
    status, _ = transport.get(CROSSREF + "/" + quote(ref["doi"], safe="/"))
    return {200: "resolves", 404: "not_found"}.get(status, "unavailable")


def score(rows, flag):
    counts = Counter((row["label"] == "HALLUCINATED", flag(row)) for row in rows)
    return {
        "tp": counts[True, True],
        "fp": counts[False, True],
        "tn": counts[False, False],
        "fn": counts[True, False],
    }


def source_coverage(rows):
    summary = {}
    for index in INDICES:
        attempts = [a for row in rows for a in row["attempts"] if a["index"] == index]
        responses = Counter(
            "invalid" if a.get("response") == "invalid" else str(a["http_status"] or "no_response")
            for a in attempts
        )
        summary[index] = {
            "rows_attempted": sum(any(a["index"] == index for a in r["attempts"]) for r in rows),
            "rows_completed": sum(index in r["looked_up"] for r in rows),
            "responses": dict(responses),
        }
    summary["rows_with_no_completed_source"] = sum(not row["looked_up"] for row in rows)
    return summary


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--fixture", type=Path, default=Path("data/hallmark/test_public.jsonl"))
    parser.add_argument(
        "--historical", type=Path, help="optional results_fixed.jsonl to compare on identical keys"
    )
    parser.add_argument("--split", choices=SPLITS, default="initial")
    parser.add_argument("--cache", type=Path, default=Path(gettempdir()) / SEED)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--indices", nargs="+", default=["crossref", "openalex", "semanticscholar"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    raw = args.fixture.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != FIXTURE_SHA256:
        parser.error(f"unexpected HALLMARK fixture SHA-256: {digest}; expected {FIXTURE_SHA256}")
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    selected, labels = sample(rows, args.split)
    fields = {row["bibtex_key"]: row["fields"] for row in rows}
    matcher_digests = {
        name: hashlib.sha256((SKILL_DIR / "scripts" / name).read_bytes()).hexdigest()
        for name in ("bibliography.py", "sources.py")
    }
    transport = Transport(args.cache, offline=args.offline)
    refs = {key: reference({**fields[key], "id": key}) for key in selected}
    if "semanticscholar" in args.indices:
        prefetch_semantic(list(refs.values()), transport)

    def measure(key):
        ref = refs[key]
        resolution, _ = lookup(ref, transport, args.indices)
        return (
            {
                "bibtex_key": key,
                "status": resolution["status"],
                "triage": classify(resolution)["label"],
                "doi_only": doi_status(ref, transport),
                "mismatch_fields": [
                    difference["field"] for difference in resolution["differences"]
                ],
                "attempts": resolution["attempts"],
                "looked_up": resolution["looked_up"],
            }
        )

    with ThreadPoolExecutor(max_workers=1 if args.offline else 8) as pool:
        measured = list(pool.map(measure, selected))
    print(f"Checked {len(measured)} references", file=sys.stderr)

    # Join gold labels only after all matcher and DOI predictions are recorded.
    for row in measured:
        row["label"], row["difficulty_tier"], row["hallucination_type"] = labels[row["bibtex_key"]]
    results = {
        "fixture_sha256": digest,
        "fixture_rows": len(rows),
        "sample": {
            "seed": SEED,
            "split": args.split,
            "valid": SAMPLE_SIZE["VALID", None],
            "hallucinated_per_tier": SAMPLE_SIZE["HALLUCINATED", 1],
            "rows": len(measured),
        },
        "matcher_sha256": matcher_digests,
        "recorded_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "cache-only" if args.offline else "keyless with cache",
        "indices": args.indices,
        "coverage": source_coverage(measured),
        "status_by_label": {
            label: dict(Counter(r["status"] for r in measured if r["label"] == label))
            for label in ("VALID", "HALLUCINATED")
        },
        "triage_by_type": {
            kind: dict(Counter(r["triage"] for r in measured if (r["hallucination_type"] or "valid") == kind))
            for kind in sorted({r["hallucination_type"] or "valid" for r in measured})
        },
        "scores": {
            "strict": score(measured, lambda r: r["status"] in STRICT_FLAGS),
            "triage": score(measured, lambda r: r["status"] != "verified"),
            "crossref_doi_only": score(measured, lambda r: r["doi_only"] == "not_found"),
        },
        "doi_only_outcomes": dict(Counter(r["doi_only"] for r in measured)),
    }
    if args.historical:
        history_raw = args.historical.read_bytes()
        history = {}
        for line in history_raw.splitlines():
            old = json.loads(line)
            history[old["bibtex_key"]] = old
        for row in measured:
            key = row["bibtex_key"]
            old = history.get(key)
            if old is None or old["label"] != row["label"]:
                parser.error(f"historical result missing or label disagrees for {key}")
            row["historical_flag"] = old["flag"]
        results["historical_sha256"] = hashlib.sha256(history_raw).hexdigest()
        results["scores"]["historical"] = score(
            measured, lambda r: r["historical_flag"] in HISTORICAL_FLAGS
        )
        results["historical_unverifiable"] = sum(
            r["historical_flag"] == "unverifiable" for r in measured
        )
    results["rows"] = measured
    output = json.dumps(results, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8", newline="\n")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
