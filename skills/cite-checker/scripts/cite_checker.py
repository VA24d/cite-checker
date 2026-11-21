"""CLI for the cite-checker Agent Skill. Uses only the Python standard library."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from concurrent.futures import ThreadPoolExecutor

from scripts.bibliography import read_input
from scripts.evidence import gate, packet, search
from scripts.shards import collect_assessments, escalations, make_shards, run_shards
from scripts.sources import (
    CLASS_ORDER,
    INDEX_NAMES,
    Transport,
    classify,
    get_source,
    identity,
    lookup,
    prefetch_arxiv,
    prefetch_semantic,
)
from scripts import numeric, registry
from scripts.structure import check as structure_check
from scripts.structure import date_checks
from scripts.web import check_web, is_web


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent) as work:
        temp = Path(work) / path.name
        temp.write_text(content, encoding="utf-8", newline="\n")
        temp.replace(path)


def jsonify(data):
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def numbered(value):
    number = int(value)
    if number < 1 or number > 20:
        raise argparse.ArgumentTypeError("expected a number from 1 to 20")
    return number


def _email(value):
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise argparse.ArgumentTypeError("expected an email address")
    return value


def _prepare(args):
    destination = args.out / "packet.json"
    if destination.exists():
        raise ValueError("packet.json already exists. Choose a fresh output directory.")
    kept = {}
    refs, claims, notices = read_input(args.input, args.bib, keep=kept)
    if args.local_only:
        notices.append("Index lookup was intentionally skipped in local-only mode.")
    if args.references_only:
        claims = []
        notices.append("Claim checks were skipped in references-only mode.")
    required = {key for claim in claims for key in claim["references"]}
    # One cache shared by every run, so repeat audits and shared references are free.
    cache = args.cache or Path(os.environ.get("CITE_CHECKER_CACHE", Path.home() / ".cache" / "cite-checker"))
    transport = Transport(cache, args.offline, args.mailto, ttl_days=args.cache_days)

    def resolve(ref):
        """Resolve one reference. Runs in a worker thread; returns its own notices."""
        key, notes = ref["id"], []
        supplied = "text" in ref or "path" in ref
        want_text = key in required and not supplied
        source = None
        if args.local_only or not any(ref[k] for k in ("doi", "arxiv_id", "raw", "title", "url")):
            resolution, rows = identity(ref, [], []), []
            if args.local_only:
                resolution["status"] = "not_assessed"
        else:
            resolution, rows = lookup(ref, transport, args.indices, want_text=want_text)
            # A URL-only entry, or one no index identified, is checked as a web page.
            if args.web and is_web(ref) and (
                not (ref.get("doi") or ref.get("arxiv_id"))
                and resolution["status"] not in ("verified", "metadata_mismatch")
            ):
                web_resolution, web_source = check_web(ref, transport)
                web_resolution["attempts"] = resolution.get("attempts", []) + web_resolution[
                    "attempts"
                ]
                resolution, rows, source = web_resolution, [], web_source
        if key in required:
            try:
                source = (
                    get_source(ref, resolution, rows, transport, args.input.parent, args.abstract_only)
                    if supplied or source is None
                    else source
                )
            except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as error:
                notes.append(f"Source for {key} could not be read: {error}")
                source = None
        else:
            source = None
        resolution["triage"] = classify(resolution)
        return key, resolution, source, notes

    if not args.local_only and "arxiv" in args.indices:
        prefetch_arxiv([r.get("arxiv_id") for r in refs], transport)
    if not args.local_only and "semanticscholar" in args.indices:
        prefetch_semantic(refs, transport)
    resolutions, bodies = {}, {}
    workers = 1 if args.offline or args.local_only else args.workers
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        # map keeps input order, so output files are identical for any worker count.
        for key, resolution, source, notes in pool.map(resolve, refs):
            resolutions[key] = resolution
            notices.extend(notes)
            if source:
                bodies[key] = source
    run = packet(refs, claims, resolutions, bodies, notices, args.top)
    run["structure"] = structure_check(
        refs, claims, resolutions, manuscript=args.input.suffix.lower() != ".bib"
    )
    run["manuscript_checks"] = _manuscript_checks(
        args, refs, claims, resolutions, kept.get("body", ""), transport
    )
    stored_sources = run.pop("sources")
    write(args.out / "sources.json", jsonify(stored_sources))
    write(destination, jsonify(run))
    return {
        "packet": str(destination),
        "references": len(refs),
        "pairs": len(run["pairs"]),
        "available_sources": len(stored_sources),
        "triage": _triage_counts(run["references"]),
        "structure_issues": len(run["structure"]),
        "manuscript_checks": _counts(i["check"] for i in run["manuscript_checks"]),
        "coverage_notes": notices,
    }


def _manuscript_checks(args, refs, claims, resolutions, body, transport):
    """Deterministic checks of the manuscript's own statements: dates, numbers the
    abstract and body state differently, and cited software packages and versions."""
    issues = date_checks(refs, claims, resolutions, time.strftime("%Y-%m-%d"))
    if body:
        latex = args.input.suffix.lower() == ".tex"
        for finding in numeric.consistency(numeric.abstract_and_body(body, latex)):
            issues.append(
                {
                    "check": finding["kind"],
                    "severity": "warning",
                    "references": [],
                    "claims": [],
                    "message": _number_message(finding),
                }
            )
    if args.registries and not args.local_only and not args.offline:
        for found in registry.check_claims(claims, transport):
            if found["status"] in ("exists", "version_exists") and not found.get("conflict"):
                continue
            if found["status"] == "lookup_unavailable":
                continue
            package = found["name"] + (f"@{found['version']}" if found.get("version") else "")
            if found["status"] == "missing" and found.get("asserts_missing"):
                continue
            if found["status"] in ("exists", "version_exists"):
                message = (f"The manuscript says {package} does not exist on {found['ecosystem']}, "
                           f"but the registry has it (latest {found.get('latest') or 'unknown'}).")
            elif found["status"] == "version_missing":
                message = (f"{package}: {found['ecosystem']} has no such version "
                           f"(latest {found.get('latest') or 'unknown'}).")
            else:
                message = f"{package} is not on {found['ecosystem']}."
            issues.append(
                {
                    "check": "package_" + found["status"],
                    "severity": "error" if found.get("conflict") else "warning",
                    "references": [],
                    "claims": [found["id"]],
                    "message": message,
                }
            )
    return issues


def _number_message(finding):
    where = finding.get("where") or "abstract"
    if finding["kind"] == "abstract_number_missing":
        return f"{finding['value']} in the {where} appears nowhere in the body ({finding.get('context', '')})."
    return (f"{finding['value']} in the {where} is stated elsewhere as {finding.get('other')} "
            f"({finding.get('context', '')}).")


def _triage_counts(references):
    counts = {}
    for row in references:
        label = (row.get("triage") or classify(row))["label"]
        counts[label] = counts.get(label, 0) + 1
    return {k: counts[k] for k in CLASS_ORDER if k in counts}


def _load(run_dir):
    metadata = read_json(run_dir / "packet.json")
    metadata["sources"] = read_json(run_dir / "sources.json")
    return metadata


def _escape(text):
    return re.sub(r"([\\\x60*_{}\[\]()#+!|<>])", r"\\\1", " ".join(str(text).split()))


def _url(url):
    return url if url and re.fullmatch(r"https?://[^\s<>()]+", url) else ""


def markdown(report):
    counts = {}
    source_levels = {}
    for row in report["claims"]:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        if row["source"] and row["source"].get("origin") == "user_supplied":
            source_levels[row["reference_id"]] = row["source"]["level"].replace("_", " ")
    lines = [
        "# Citation audit",
        "",
        "## Coverage",
        "",
        f"- {len(report['references'])} references and {len(report['claims'])} claim-source pairs",
        "- Source quotes were checked against frozen text. The host agent assessed claim support.",
    ]
    lines.extend(f"- {_escape(note)}" for note in report["coverage_notes"])
    if counts:
        lines.append(
            "- " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(counts.items()))
        )
    lines.extend(
        [
            "",
            "## References",
            "",
            "| Citation | Triage | Metadata status | Record or issue |",
            "|---|---|---|---|",
        ]
    )
    for row in report["references"]:
        source = row.get("source")
        if source:
            title, url = _escape(source["title"]), _url(source.get("url"))
            record = f"[{title}]({url})" if url else title
            if source.get("retracted"):
                record += " · Index marks this work as retracted"
        else:
            record = ", ".join(_escape(r["title"]) for r in row.get("candidates", []))
            if not record and row.get("reference"):
                ref = row["reference"]
                record = _escape(ref.get("title") or ref.get("doi") or ref.get("arxiv_id") or "")
                if record:
                    record = "Supplied reference: " + record
        if row["id"] in source_levels:
            label = f"Supplied source available ({source_levels[row['id']]})"
            record = f"{record} · {label}" if record else label
        if row.get("differences"):
            record += " · " + ", ".join(
                f"{item['field']} given as {_escape(item['stated'])}, "
                f"record has {_escape(item['record'])}"
                for item in row["differences"]
            )
        triage = (row.get("triage") or classify(row))["label"].replace("_", " ")
        for advice in row.get("advisories", []):
            if advice.get("matches") is True:
                continue
            detail = advice.get("doi") or advice.get("url") or advice.get("title") or ""
            record += f" · {_escape(advice['message'])}"
            if detail:
                record += f" ({_escape(detail)})"
        lines.append(
            f"| {_escape(row['id'])} | {triage} | {row['status'].replace('_', ' ')} | {record} |"
        )
    if report.get("structure"):
        lines.extend(["", "## Bibliography structure", ""])
        for issue in report["structure"]:
            keys = ", ".join(_escape(k) for k in issue["references"])
            lines.append(
                f"- **{issue['severity']}** {_escape(issue['message'])}"
                + (f" ({keys})" if keys else "")
            )
    if report.get("manuscript_checks"):
        lines.extend(["", "## Manuscript checks", ""])
        for issue in report["manuscript_checks"]:
            where = ", ".join(_escape(k) for k in issue.get("references", []) + issue.get("claims", []))
            lines.append(
                f"- **{issue['severity']}** {_escape(issue['message'])}"
                + (f" ({where})" if where else "")
            )
    if report["claims"]:
        lines.extend(["", "## Claim support"])
    for row in report["claims"]:
        label = row["verdict"].replace("_", " ").capitalize()
        lines.extend(
            [
                "",
                f"### {_escape(row['claim_id'])} · {row['pair_id']} · {label}",
                "",
                f"**Claim** {_escape(row['claim'])}",
                "",
                f"**Citation** {_escape(row['reference_id'])}",
                "",
                _escape(row["reason"]),
            ]
        )
        if row.get("caveat"):
            lines.extend(["", f"**Caveat** {_escape(row['caveat'])}"])
        if row.get("secondary"):
            lines.extend(["", f"**Secondary citation** The source attributes this to other work "
                          f"({_escape(row['secondary'])}). Consider citing the original."])
        for e in row["evidence"]:
            url = _url(e["url"])
            source_label = f"{e['origin']} · {e['level'].replace('_', ' ')}"
            if url:
                source_label = f"[{_escape(source_label)}]({url})"
            lines.extend(
                [
                    "",
                    f"> {_escape(e['quote'])}",
                    "",
                    f"{source_label} · characters {e['start']} to {e['end']}",
                ]
            )
        lines.extend(f"- {_escape(note)}" for note in row["gate_issues"])
    return "\n".join(lines) + "\n"


def _finalize(args):
    proposals = collect_assessments(args.assessments) if args.assessments else []
    report = gate(_load(args.run), proposals)
    write(args.run / "report.json", jsonify(report))
    write(args.run / "report.md", markdown(report))
    # A pair already answered by the strong model is final; re-escalating it would loop.
    answered = {p["pair_id"] for p in proposals if p.get("escalated")}
    escalate = escalations(report) - answered if proposals else set()
    summary = {
        "report": str(args.run / "report.md"),
        "json": str(args.run / "report.json"),
        "pairs": len(report["claims"]),
        "verdicts": _counts(c["verdict"] for c in report["claims"]),
        "triage": _triage_counts(report["references"]),
        "structure_issues": len(report.get("structure", [])),
        "quote_validation_failures": sum(bool(c["gate_issues"]) for c in report["claims"]),
        "not_assessed": sum(c["verdict"] == "not_assessed" for c in report["claims"]),
        "escalated_answers": len(answered),
    }
    if escalate:
        shards = make_shards(args.run, size=args.escalate_size, only=escalate, name="escalate")
        summary["escalate"] = {"pairs": len(escalate), "shards": shards["shards"]}
    return summary


def _counts(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="parse, resolve, and capture cited sources")
    prep.add_argument("input", type=Path)
    prep.add_argument("--out", type=Path, required=True)
    prep.add_argument("--bib", type=Path, action="append", default=[])
    prep.add_argument("--indices", choices=tuple(INDEX_NAMES), nargs="+", default=list(INDEX_NAMES))
    prep.add_argument(
        "--cache", type=Path, help="HTTP cache folder (default ~/.cache/cite-checker)"
    )
    prep.add_argument(
        "--cache-days", type=numbered, default=30, help="refetch cached answers older than this"
    )
    prep.add_argument("--mailto", type=_email, help="optional scholarly API contact email")
    prep.add_argument(
        "--no-registries",
        dest="registries",
        action="store_false",
        help="skip npm, PyPI, crates.io and GitHub checks of packages named in claims",
    )
    prep.add_argument("--top", type=numbered, default=5)
    prep.add_argument("--abstract-only", action="store_true")
    prep.add_argument(
        "--workers", type=numbered, default=8, help="references resolved in parallel"
    )
    prep.add_argument(
        "--no-web",
        dest="web",
        action="store_false",
        help="do not fetch cited web pages, archived copies or CITATION.cff",
    )
    prep.add_argument("--references-only", action="store_true")
    modes = prep.add_mutually_exclusive_group()
    modes.add_argument(
        "--offline", action="store_true", help="use cached lookups and supplied files only"
    )
    modes.add_argument(
        "--local-only", action="store_true", help="use supplied sources and skip all index lookups"
    )
    prep.set_defaults(action=_prepare)
    find = commands.add_parser(
        "search", help="read alternative passages from the same frozen source"
    )
    find.add_argument("run", type=Path)
    find.add_argument("pair")
    find.add_argument("query")
    find.add_argument("--top", type=numbered, default=5)
    find.set_defaults(action=lambda args: search(_load(args.run), args.pair, args.query, args.top))
    shard = commands.add_parser(
        "shard", help="split claim checks into compact batches for cheap judge models"
    )
    shard.add_argument("run", type=Path)
    shard.add_argument("--size", type=numbered, default=8, help="pairs per shard")
    shard.add_argument("--top", type=numbered, default=3, help="passages per pair")
    shard.set_defaults(action=lambda a: make_shards(a.run, a.size, a.top))
    judge = commands.add_parser(
        "judge", help="judge shards with any model CLI (prompt passed as last argument)"
    )
    judge.add_argument("run", type=Path)
    judge.add_argument(
        "--runner",
        required=True,
        help='e.g. "claude -p --model haiku" or "agy --sandbox --mode plan '
        '--model gemini-3.8-flash-high --dangerously-skip-permissions -p"',
    )
    judge.add_argument("--jobs", type=numbered, default=4)
    judge.add_argument("--pattern", default="shard-*.json", help="e.g. escalate-*.json")
    judge.add_argument("--price-in", type=float, default=0.0, help="USD per million input tokens")
    judge.add_argument("--price-out", type=float, default=0.0, help="USD per million output tokens")
    judge.add_argument(
        "--structured",
        action="store_true",
        help="ask for direction, numbers and clause checks; the verdict is derived in Python",
    )
    judge.add_argument(
        "--votes", type=numbered, default=1, help="judge each shard N times and take the majority"
    )
    judge.set_defaults(
        action=lambda a: run_shards(
            a.run,
            a.runner,
            a.jobs,
            pattern=a.pattern,
            price_in=a.price_in,
            price_out=a.price_out,
            structured=a.structured,
            votes=a.votes,
        )
    )
    finalize = commands.add_parser("finalize", help="validate proposed quotes and write a report")
    finalize.add_argument("run", type=Path)
    finalize.add_argument(
        "--assessments",
        type=Path,
        action="append",
        help="assessment file or folder; repeat to layer escalated answers on top",
    )
    finalize.add_argument("--escalate-size", type=numbered, default=6)
    finalize.set_defaults(action=_finalize)
    args = parser.parse_args(argv)
    try:
        sys.stdout.buffer.write(jsonify(args.action(args)).encode("utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"cite-checker: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
