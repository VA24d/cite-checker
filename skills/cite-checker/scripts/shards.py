"""Split claim checks into small batches for cheap judge models, and collect their answers.

A shard holds only what a judge needs for each pair: the claim, a one-line citation, the
top passages and deterministic hints. Reference metadata, attempts and source text stay
out of model context. Pairs without a captured source are never sent: the gate already
marks them insufficient.
"""

import json
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.numeric import compare as number_facts

RULES = Path(__file__).resolve().parent.parent / "references" / "judging.md"
STRUCTURED_RULES = RULES.with_name("judging-structured.md")
from collections import Counter  # noqa: E402
NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?\s?%?")


def _load(run):
    packet = json.loads((run / "packet.json").read_text(encoding="utf-8"))
    sources = json.loads((run / "sources.json").read_text(encoding="utf-8"))
    return packet, sources


def _cite(ref):
    data = ref.get("source") or ref.get("reference") or {}
    authors = data.get("authors") or []
    parts = authors[0].split(",")[0].split() if authors else []
    first = parts[-1] if parts else ""
    years = data.get("years") or ([data["year"]] if data.get("year") else [""])
    year = years[0]
    title = (data.get("title") or "")[:90]
    return " · ".join(str(x) for x in (first, year, title) if x)


def _hints(pair, text):
    claim = pair["claim"]
    numbers = [n.strip() for n in NUMBER.findall(claim)]
    flat = " ".join(text.split())
    absent = [n for n in dict.fromkeys(numbers) if n.rstrip("%").strip() not in flat]
    hint = {}
    if absent:
        # Arithmetic is where cheap judges slip, so the script does it: a claim number
        # missing from the text may still be computed from two source numbers.
        facts = number_facts(claim, text)["summary"]
        derived = {d["value"]: d["formula"] for d in facts["derived"]}
        absent = [n for n in absent if n not in derived]
        if absent:
            hint["numbers_absent_from_source"] = absent
        if derived:
            hint["numbers_computed_from_source"] = derived
        if facts["mismatch"]:
            hint["source_states_other_values"] = {m["value"]: m["source_values"] for m in facts["mismatch"]}
    if pair.get("sentence"):
        hint["sentence"] = pair["sentence"]
    if pair.get("first_person"):
        hint["first_person"] = True
    return hint


def _merge(passages):
    """Join overlapping chunks so shared text is sent once."""
    spans = sorted(passages, key=lambda p: p["start"])
    out = []
    for p in spans:
        if out and p["start"] <= out[-1]["end"]:
            last = out[-1]
            if p["end"] > last["end"]:
                last["text"] += p["text"][last["end"] - p["start"] :]
                last["end"] = p["end"]
        else:
            out.append(dict(p))
    return [p["text"] for p in out]


def compact_pair(pair, refs, sources, top):
    source = sources[pair["reference_id"]]
    passages = pair["passages"][:top] if top else pair["passages"]
    return {
        "id": pair["id"],
        "claim": pair["claim"],
        "cite": _cite(refs.get(pair["reference_id"], {})),
        "level": source["level"],
        "p": _merge(passages),
        **({"hint": h} if (h := _hints(pair, source["text"])) else {}),
    }


def make_shards(run, size=8, top=3, only=None, name="shard"):
    """Write shards/<name>-NN.json. Returns the shard paths and the skipped pair count."""
    packet, sources = _load(run)
    refs = {r["id"]: r for r in packet["references"] + packet.get("suggested_references", [])}
    pairs = [p for p in packet["pairs"] if p["reference_id"] in sources]
    if only is None:
        pairs = [p for p in pairs if not p.get("candidate")]
    if only is not None:
        pairs = [p for p in pairs if p["id"] in only]
    # Keep pairs that cite one source together so a judge reads related passages at once.
    pairs.sort(key=lambda p: (p["reference_id"], p["id"]))
    folder = run / "shards"
    folder.mkdir(exist_ok=True)
    for old in folder.glob(f"{name}-*.json"):
        old.unlink()
    paths = []
    for start in range(0, len(pairs), size):
        batch = [compact_pair(p, refs, sources, top) for p in pairs[start : start + size]]
        path = folder / f"{name}-{start // size + 1:02d}.json"
        path.write_text(
            json.dumps({"pairs": batch}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        paths.append(path)
    return {
        "shards": [str(p) for p in paths],
        "pairs_sent": len(pairs),
        "pairs_without_source": len(packet["pairs"]) - len(
            [p for p in packet["pairs"] if p["reference_id"] in sources]
        ),
        "approx_input_tokens": sum(p.stat().st_size for p in paths) // 4
        + len(paths) * (RULES.stat().st_size // 4),
    }


def derive_verdict(row):
    """Turn structured answers (direction, numbers, extra clauses) into a verdict.

    Small models extract these fields about as well as large ones; the verdict rules are
    where they diverge, so the rules live here instead of in the prompt."""
    if row.get("verdict") in ("supported", "partially_supported", "contradicted", "insufficient"):
        return row
    direction = str(row.get("direction", "none")).lower()
    numbers = str(row.get("numbers", "n/a")).lower()
    extras = row.get("extra_clauses") or {}
    unsupported = [k for k, v in extras.items() if v is False or str(v).lower() == "false"]
    if not row.get("quotes"):
        verdict = "insufficient"
    elif direction == "opposite" or numbers == "mismatch":
        verdict = "contradicted"
    elif direction == "same" and numbers in ("match", "n/a") and not unsupported:
        verdict = "supported"
    elif direction == "same":
        verdict = "partially_supported"
    else:
        verdict = "insufficient"
    reason = str(row.get("reason") or "").strip() or f"Direction {direction}, numbers {numbers}."
    if unsupported and verdict == "partially_supported":
        reason += " Unsupported: " + "; ".join(map(str, unsupported)) + "."
    return {**row, "verdict": verdict, "reason": reason}


def vote(answers):
    """Majority verdict over several runs of the same shard. A pair whose winning verdict
    has no strict majority is marked `split` so finalize escalates it."""
    by_pair = {}
    for rows in answers:
        for row in rows:
            by_pair.setdefault(row["pair_id"], []).append(derive_verdict(row))
    merged = []
    for pair_id, rows in by_pair.items():
        counts = Counter(r["verdict"] for r in rows)
        (winner, top), = counts.most_common(1)
        tied = sum(1 for c in counts.values() if c == top) > 1
        chosen = next(r for r in rows if r["verdict"] == winner)
        merged.append(
            {
                **chosen,
                "votes": dict(counts),
                "split": tied or top * 2 <= len(answers),
            }
        )
    return merged


def prompt_for(shard_path, run, structured=False):
    rules = (STRUCTURED_RULES if structured else RULES).read_text(encoding="utf-8")
    shard = Path(shard_path).read_text(encoding="utf-8")
    return (
        f"{rules}\n\nRun directory: {run}\n"
        "Return ONLY a JSON array, one object per pair, no prose.\n\n"
        f"SHARD:\n{shard}\n"
    )


def _parse(stdout, ids):
    start, end = stdout.find("["), stdout.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("no JSON array in judge output")
    rows = json.loads(stdout[start : end + 1])
    if not isinstance(rows, list):
        raise ValueError("judge output is not an array")
    kept = [r for r in rows if isinstance(r, dict) and r.get("pair_id") in ids]
    if not kept:
        raise ValueError("judge output names none of the shard's pairs")
    return kept


def run_shards(
    run,
    runner,
    jobs=4,
    retries=2,
    timeout=900,
    pattern="shard-*.json",
    price_in=0.0,
    price_out=0.0,
    backoff=30,
    structured=False,
    votes=1,
):
    """Judge every shard with an external CLI, e.g. `claude -p --model haiku` or
    `agy --sandbox --mode plan --model gemini-3.8-flash-high --dangerously-skip-permissions -p`.
    The prompt is passed as the final argument. Existing answers are kept (resumable)."""
    out_dir = run / "assessments"
    out_dir.mkdir(exist_ok=True)
    command = shlex.split(runner)
    shards = sorted((run / "shards").glob(pattern))

    meter = []

    def one(path):
        target = out_dir / path.name
        if target.exists():
            return path.name, "cached"
        ids = {p["id"] for p in json.loads(path.read_text(encoding="utf-8"))["pairs"]}
        prompt = prompt_for(path, run, structured)
        answers, error = [], ""
        for _ in range(votes):
            rows, error = ask(path, ids, prompt)
            if rows:
                answers.append(rows)
        if not answers:
            return path.name, f"failed: {error}"
        rows = vote(answers) if (votes > 1 or structured) else answers[0]
        target.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        missing = len(ids) - len({r["pair_id"] for r in rows})
        note = f", {len(answers)}/{votes} votes" if votes > 1 else ""
        return path.name, ("ok" if not missing else f"ok, {missing} pairs unanswered") + note

    def ask(path, ids, prompt):
        error = ""
        for attempt in range(retries + 1):
            started = time.monotonic()
            try:
                done = subprocess.run(
                    [*command, prompt],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                # Estimated at four characters per token; CLIs do not all report usage.
                meter.append(
                    {
                        "shard": path.name,
                        "attempt": attempt + 1,
                        "seconds": round(time.monotonic() - started, 1),
                        "input_tokens": len(prompt) // 4,
                        "output_tokens": len(done.stdout) // 4,
                    }
                )
                rows = _parse(done.stdout, ids)
            except (subprocess.SubprocessError, ValueError, OSError) as exc:
                error = str(exc)[:200]
                # Most failures are rate limits or overload; wait before trying again.
                if attempt < retries:
                    time.sleep(backoff * (attempt + 1))
                continue
            return rows, ""
        return None, error

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = dict(pool.map(one, shards))
    tokens_in = sum(m["input_tokens"] for m in meter)
    tokens_out = sum(m["output_tokens"] for m in meter)
    usage = {
        "runner": runner,
        "calls": len(meter),
        "input_tokens_est": tokens_in,
        "output_tokens_est": tokens_out,
        "model_seconds": round(sum(m["seconds"] for m in meter), 1),
        "usd_est": round((tokens_in * price_in + tokens_out * price_out) / 1e6, 4),
        "per_call": sorted(meter, key=lambda m: (m["shard"], m["attempt"])),
    }
    # One meter file per pass, so first-pass and escalation costs can be compared.
    meter_path = run / f"meter-{pattern.split('-')[0].strip('*')}.json"
    meter_path.write_text(json.dumps(usage, indent=1), encoding="utf-8")
    return {
        "assessments": str(out_dir),
        "shards": results,
        "usage": {k: v for k, v in usage.items() if k != "per_call"},
    }


def collect_assessments(paths):
    """Merge assessment arrays from files or folders. Later files override earlier ones,
    so an escalated re-judgment replaces the cheap model's answer."""
    files = []
    for order, path in enumerate(map(Path, paths)):
        found = sorted(path.glob("*.json")) if path.is_dir() else [path]
        files.extend((order, f) for f in found)
    # Escalated answers apply last, whichever folder they came from, so they replace the
    # first pass; otherwise later arguments override earlier ones.
    files = [f for _, f in sorted(files, key=lambda t: (t[1].name.startswith("escalate"), t[0], t[1].name))]
    merged = {}
    for file in files:
        rows = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError(f"{file}: assessments JSON must be an array")
        for row in rows:
            if isinstance(row, dict) and "pair_id" in row:
                row = derive_verdict(row)
                if file.name.startswith("escalate"):
                    row["escalated"] = True
                merged[row["pair_id"]] = row
    return list(merged.values())


# Claims whose truth turns on a number, a direction or a negation. An `insufficient` call
# on such a claim, with source text present, is the cheap model's most common miss.
DECISIVE_CLAIM = re.compile(
    r"\d|%|\b(?:increase[sd]?|decrease[sd]?|reduc\w*|rais\w*|lower\w*|higher|greater|"
    r"less|more|fewer|improv\w*|worsen\w*|promot\w*|inhibit\w*|prevent\w*|caus\w*|"
    r"associat\w*|correlat\w*|no|not|never|without|fail\w*|unable)\b",
    re.I,
)


def escalations(report):
    """Pairs a stronger model should re-judge: failed quote gates, decisive negatives, and
    `insufficient` calls on claims that state a number, direction or negation."""
    return {
        c["pair_id"]
        for c in report["claims"]
        if c["gate_issues"]
        or c["proposed_verdict"] in ("contradicted", "partially_supported")
        or c["verdict"] == "not_assessed"
        or c.get("split")
        or (
            c["proposed_verdict"] == "insufficient"
            and c["source"] is not None
            and DECISIVE_CLAIM.search(c["claim"])
        )
    }

