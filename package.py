"""Build release archives and check release metadata.

python package.py              build dist/cite-checker.zip (the skill folder, for Claude.ai
                               or other hosts) and dist/cite-checker-plugin.zip (the Claude
                               Code plugin: manifest, agents, commands and skill)
python package.py --check      validate the plugin manifests and the skill front matter
python package.py --check --tag v2.1.0
                               also require the tag to match the plugin version
python package.py --notes v2.1.0
                               print that version's section of CHANGELOG.md
"""

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL = ROOT / "skills" / "cite-checker"
PLUGIN_PARTS = (".claude-plugin", "agents", "commands", "skills", "README.md", "CHANGELOG.md", "LICENSE", "CITATION.cff")


def _files(base):
    if base.is_file():
        return [base]
    return sorted(
        p
        for p in base.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )


def _write(out, entries):
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in entries:
            # Fixed timestamps keep the archive byte-identical across builds.
            info = zipfile.ZipInfo(name)
            info.date_time = (2026, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return out, len(entries)


def build(out=ROOT / "dist" / "cite-checker.zip"):
    entries = [(str(Path("cite-checker") / p.relative_to(SKILL)), p) for p in _files(SKILL)]
    entries.append(("cite-checker/LICENSE", ROOT / "LICENSE"))
    return _write(out, entries)


def build_plugin(out=ROOT / "dist" / "cite-checker-plugin.zip"):
    entries = [
        (str(Path("cite-checker") / p.relative_to(ROOT)), p)
        for part in PLUGIN_PARTS
        if (ROOT / part).exists()
        for p in _files(ROOT / part)
    ]
    return _write(out, entries)


def check(tag=None):
    """Return a list of problems with the release metadata."""
    problems = []
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    version = plugin.get("version", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        problems.append(f"plugin.json version {version!r} is not MAJOR.MINOR.PATCH")
    entry = next((p for p in market.get("plugins", []) if p.get("name") == plugin.get("name")), None)
    if entry is None:
        problems.append("marketplace.json does not list the plugin by name")
    elif entry.get("version") not in (None, version):
        problems.append(f"marketplace.json version {entry['version']} != plugin.json {version}")
    if tag is not None and tag.removeprefix("v") != version:
        problems.append(f"tag {tag} does not match plugin version {version}")
    front = re.match(r"---\n(.*?)\n---\n", (SKILL / "SKILL.md").read_text(encoding="utf-8"), re.S)
    if not front or not re.search(r"(?m)^name: cite-checker$", front[1]) or "description:" not in front[1]:
        problems.append("SKILL.md front matter needs name: cite-checker and a description")
    for agent in (ROOT / "agents").glob("*.md"):
        if not agent.read_text(encoding="utf-8").startswith("---\n"):
            problems.append(f"{agent.name} has no front matter")
    if not (ROOT / "LICENSE").exists():
        problems.append("LICENSE is missing")
    if version and f"## {version}" not in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"):
        problems.append(f"CHANGELOG.md has no '## {version}' section")
    return problems


def notes(tag):
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version = re.escape(tag.removeprefix("v"))
    match = re.search(rf"(?ms)^## {version}\b[^\n]*\n(.*?)(?=^## |\Z)", text)
    return match[1].strip() + "\n" if match else ""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="validate release metadata")
    parser.add_argument("--tag", help="with --check: the release tag, e.g. v2.1.0")
    parser.add_argument("--notes", metavar="TAG", help="print the CHANGELOG section for TAG")
    args = parser.parse_args()
    if args.notes:
        sys.stdout.write(notes(args.notes) or sys.exit(f"CHANGELOG.md has no section for {args.notes}"))
    elif args.check:
        found = check(args.tag)
        for problem in found:
            print("error:", problem, file=sys.stderr)
        sys.exit(1 if found else 0)
    else:
        for path, count in (build(), build_plugin()):
            print(f"{path} ({count} files)")
