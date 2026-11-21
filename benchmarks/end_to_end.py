"""Score a finalized audit of the end-to-end fixture against its answer key.

    python cite-checker/skills/cite-checker/scripts/cite_checker.py prepare \
        cite-checker/benchmarks/fixtures/end-to-end/paper.md \
        --bib cite-checker/benchmarks/fixtures/end-to-end/refs.bib --out <run-dir>
    python cite-checker/benchmarks/end_to_end.py <run-dir>/report.json

The manuscript cites seven real arXiv papers and one invented work. One real
reference gives the wrong venue. Claims include exact results, altered numbers,
facts the cited paper never addresses, and a claim citing the wrong paper.
"""

import argparse
import json
from pathlib import Path

GOLD = Path(__file__).resolve().parent / "fixtures" / "end-to-end" / "gold.json"
FLAGS = {"metadata_mismatch", "not_found"}


def score(report):
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    refs = {row["id"]: row["status"] for row in report["references"]}
    claims = {row["claim_id"]: row for row in report["claims"]}
    reference_rows = [
        {
            "id": key,
            "expected": label,
            "status": refs.get(key),
            "correct": (refs.get(key) in FLAGS) == (label != "valid"),
        }
        for key, label in gold["references"].items()
    ]
    claim_rows = [
        {
            "id": key,
            "expected": label,
            "verdict": claims.get(key, {}).get("verdict", "missing"),
            "correct": claims.get(key, {}).get("verdict") == label,
        }
        for key, label in gold["claims"].items()
    ]
    return {
        "references_correct": sum(r["correct"] for r in reference_rows),
        "references": len(reference_rows),
        "claims_correct": sum(r["correct"] for r in claim_rows),
        "claims": len(claim_rows),
        "quote_validation_failures": sum(bool(c.get("gate_issues")) for c in claims.values()),
        "misses": [r for r in reference_rows + claim_rows if not r["correct"]],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    print(json.dumps(score(report), indent=2))


if __name__ == "__main__":
    main()
