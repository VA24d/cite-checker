"""Reproducible SciFact dev evaluation with blind manifests and isolated scoring."""

import argparse
import hashlib
import json
import sys
import tarfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "cite-checker"))
from scripts.evidence import packet, rank

SHA256 = "11c621288d41ac144d29b13b0f8503b3820b7d6e8b1f6ff24dff335c196d76be"
LABELS = {"SUPPORT": "supported", "CONTRADICT": "contradicted"}
CLASSES = ("supported", "contradicted", "insufficient")


def load(archive):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValueError("SciFact archive digest differs from the benchmark's pinned snapshot")
    with tarfile.open(archive) as tf:
        claims = [json.loads(line) for line in tf.extractfile("data/claims_dev.jsonl")]
        corpus = {
            d["doc_id"]: d
            for line in tf.extractfile("data/corpus.jsonl")
            if (d := json.loads(line))
        }
    rows = []
    for claim in claims:
        for source_id in claim["cited_doc_ids"]:
            evidence = claim["evidence"].get(str(source_id), [])
            if claim["evidence"] and not evidence:
                continue
            labels = {e["label"] for e in evidence}
            if len(labels) > 1:
                continue
            source = corpus[source_id]
            sentences = source["abstract"]
            offsets = []
            cursor = 0
            for sentence in sentences:
                offsets.append((cursor, cursor + len(sentence)))
                cursor += len(sentence) + 1
            rows.append(
                (
                    claim["id"],
                    source_id,
                    claim["claim"],
                    source["title"],
                    "\n".join(sentences),
                    LABELS[next(iter(labels))] if labels else "insufficient",
                    [[offsets[i] for i in e["sentences"]] for e in evidence],
                )
            )
    return rows


def stable(row):
    return hashlib.sha256(f"cite-checker-scifact-v1/{row[0]}/{row[1]}".encode()).digest()


def sample(rows, dev_per_class, holdout_per_class):
    dev, holdout = [], []
    seen = set()
    for label in CLASSES:
        selected = []
        for row in sorted((r for r in rows if r[5] == label), key=stable):
            if row[0] not in seen:
                seen.add(row[0])
                selected.append(row)
            if len(selected) == dev_per_class + holdout_per_class:
                break
        dev.extend(selected[:dev_per_class])
        holdout.extend(selected[dev_per_class:])
    return sorted(dev, key=stable), sorted(holdout, key=stable)


def confirmation_sample(rows, previous):
    previous_claims = {row[0] for row in previous}
    previous_sources = {row[1] for row in previous}
    fresh = [r for r in rows if r[0] not in previous_claims and r[1] not in previous_sources]
    _, chosen = sample(fresh, 0, 12)
    return chosen


def fresh_sample(rows, previous, per_class=16):
    """Pairs sharing no claim or cited document with any earlier sample, for Claude runs."""
    previous_claims = {row[0] for row in previous}
    previous_sources = {row[1] for row in previous}
    pool = [r for r in rows if r[0] not in previous_claims and r[1] not in previous_sources]

    def key(row):
        return hashlib.sha256(f"claude-fresh-v1/{row[0]}/{row[1]}".encode()).digest()

    chosen, claims, sources = [], set(), set()
    for label in CLASSES:
        picked = 0
        for row in sorted((r for r in pool if r[5] == label), key=key):
            if row[0] in claims or row[1] in sources:
                continue
            claims.add(row[0])
            sources.add(row[1])
            chosen.append(row)
            picked += 1
            if picked == per_class:
                break
    return sorted(chosen, key=key)


def retrieval(rows):
    scored = [r for r in rows if r[6]]
    result = {}
    for k in (1, 3, 5):
        any_hit = complete = 0
        for _, _, claim, _, text, _, gold in scored:
            spans = rank(text, claim, k)
            matched = [
                [any(p["start"] <= a and p["end"] >= b for p in spans) for a, b in alt]
                for alt in gold
            ]
            any_hit += any(map(any, matched))
            complete += any(map(all, matched))
        result[str(k)] = {
            "n": len(scored),
            "any_rationale_sentence": any_hit,
            "complete_rationale_set": complete,
        }
    return result


def packet_comparison(rows):
    """Compare ranked top-five excerpts with the current initial evidence packet."""
    totals = {mode: Counter() for mode in ("ranked", "packet")}
    for _, _, claim, _, body, _, gold in rows:
        run = packet(
            [{"id": "ref"}],
            [{"id": "item", "text": claim, "references": ["ref"]}],
            {},
            {"ref": {"level": "abstract", "text": body}},
            [],
        )
        for mode, spans in (
            ("ranked", rank(body, claim, 5)),
            ("packet", run["pairs"][0]["passages"]),
        ):
            totals[mode]["characters"] += sum(len(span["text"]) for span in spans)
            if gold:
                totals[mode]["annotated_pairs"] += 1
                totals[mode]["complete_rationales"] += any(
                    all(any(s["start"] <= a and s["end"] >= b for s in spans) for a, b in alt)
                    for alt in gold
                )
    return totals


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fixture(rows, manifest, truth):
    refs, claims, gold = [], [], {}
    for i, (orig, doc, claim, title, text, verdict, rationales) in enumerate(rows, 1):
        rid, cid = f"ref-{i:03d}", f"item-{i:03d}"
        refs.append({"id": rid, "title": title, "text": text, "level": "abstract"})
        claims.append({"id": cid, "text": claim, "references": [rid]})
        gold[cid] = {"verdict": verdict, "source_ids": [orig, doc], "rationales": rationales}
    write(manifest, {"references": refs, "claims": claims})
    write(truth, gold)


def score(truth, report):
    expected = json.loads(truth.read_text(encoding="utf-8"))
    data = json.loads(report.read_text(encoding="utf-8"))
    found = {c["claim_id"]: c for c in data["claims"]}
    if len(found) != len(data["claims"]) or found.keys() - expected.keys():
        raise ValueError("duplicate or unknown benchmark claim id")
    scores = {}
    confusion = Counter()
    evidence_match = evidence_total = 0
    wrong = []
    for key, target in expected.items():
        row = found.get(key, {})
        verdict = row.get("verdict", "not_assessed")
        wanted = target["verdict"]
        confusion[wanted, verdict] += 1
        if verdict == wanted and target["rationales"]:
            evidence_total += 1
            evidence_match += any(
                e["start"] < end and e["end"] > start
                for e in row.get("evidence", [])
                for rationale in target["rationales"]
                for start, end in rationale
            )
        if verdict != wanted:
            wrong.append(
                {
                    "claim_id": key,
                    "gold": wanted,
                    "prediction": verdict,
                    "reason": row.get("reason"),
                }
            )
    for label in CLASSES:
        tp = confusion[label, label]
        fp = sum(n for (a, b), n in confusion.items() if a != label and b == label)
        fn = sum(n for (a, b), n in confusion.items() if a == label and b != label)
        scores[label] = {
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "f1": 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 0,
        }
    return {
        "n": len(expected),
        "correct": len(expected) - len(wrong),
        "macro_f1": sum(v["f1"] for v in scores.values()) / len(scores),
        "scores": scores,
        "wrong": wrong,
        "rationale_overlap_on_correct_definitive": {"matched": evidence_match, "n": evidence_total},
        "quote_gate_failures": sum(bool(r.get("gate_issues")) for r in found.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("archive", type=Path)
    prep.add_argument("out", type=Path)
    eval_ = commands.add_parser("score")
    eval_.add_argument("truth", type=Path)
    eval_.add_argument("report", type=Path)
    args = parser.parse_args()
    if args.command == "score":
        result = score(args.truth, args.report)
    else:
        rows = load(args.archive)
        dev, holdout = sample(rows, 6, 8)
        confirmation = confirmation_sample(rows, [*dev, *holdout])
        fresh = fresh_sample(rows, [*dev, *holdout, *confirmation])
        samples = [("dev", dev), ("holdout", holdout), ("confirmation", confirmation)]
        for name, records in [*samples, ("fresh", fresh)]:
            fixture(records, args.out / f"{name}.json", args.out / "gold" / f"{name}.json")
        result = {
            "pool_pairs": len(rows),
            "dev_n": len(dev),
            "holdout_n": len(holdout),
            "confirmation_n": len(confirmation),
            "fresh_n": len(fresh),
            "retrieval": retrieval(rows),
            "initial_packet_comparison": packet_comparison(rows),
            "archive_sha256": SHA256,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
