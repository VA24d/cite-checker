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

RULES = Path(__file__).resolve().parent.parent / "references" / "judging.md"
NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?\s?%?")


def _load(run):
    packet = json.loads((run / "packet.json").read_text(encoding="utf-8"))
    sources = json.loads((run / "sources.json").read_text(encoding="utf-8"))
    return packet, sources


def _cite(ref):
    data = ref.get("source") or ref.get("reference") or {}
    authors = data.get("authors") or []
    first = authors[0].split(",")[0].split()[-1] if authors else ""
    years = data.get("years") or ([data["year"]] if data.get("year") else [""])
    year = years[0]
    title = (data.get("title") or "")[:90]
    return " · ".join(str(x) for x in (first, year, title) if x)


def _hints(claim, text):
    numbers = [n.strip() for n in NUMBER.findall(claim)]
    flat = " ".join(text.split())
    absent = [n for n in dict.fromkeys(numbers) if n.rstrip("%").strip() not in flat]
    return {"numbers_absent_from_source": absent} if absent else {}


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
        **({"hint": h} if (h := _hints(pair["claim"], source["text"])) else {}),
    }


def make_shards(run, size=8, top=3, only=None, name="shard"):
    """Write shards/<name>-NN.json. Returns the shard paths and the skipped pair count."""
    packet, sources = _load(run)
    refs = {r["id"]: r for r in packet["references"]}
    pairs = [p for p in packet["pairs"] if p["reference_id"] in sources]
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


def prompt_for(shard_path, run):
    rules = RULES.read_text(encoding="utf-8")
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
        error = ""
        prompt = prompt_for(path, run)
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
            target.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
            missing = len(ids) - len({r["pair_id"] for r in rows})
            return path.name, "ok" if not missing else f"ok, {missing} pairs unanswered"
        return path.name, f"failed: {error}"

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
    for path in paths:
        path = Path(path)
        found = path.glob("*.json") if path.is_dir() else [path]
        # Escalated answers sort last so they replace the first pass.
        files.extend(sorted(found, key=lambda f: (f.name.startswith("escalate"), f.name)))
    merged = {}
    for file in files:
        rows = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError(f"{file}: assessments JSON must be an array")
        for row in rows:
            if isinstance(row, dict) and "pair_id" in row:
                merged[row["pair_id"]] = row
    return list(merged.values())


def escalations(report):
    """Pairs a stronger model should re-judge: failed quote gates and decisive negatives."""
    return {
        c["pair_id"]
        for c in report["claims"]
        if c["gate_issues"]
        or c["proposed_verdict"] in ("contradicted", "partially_supported")
        or c["verdict"] == "not_assessed"
    }

