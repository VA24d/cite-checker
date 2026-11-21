"""Install the standalone cite-checker skill for one or more local hosts."""

import argparse
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent / "skills" / "cite-checker"
HOST_DIRS = {"codex": ".agents", "claude": ".claude", "gemini": ".gemini", "t3": ".agents"}
GEMINI_COMMAND = '''description = "Check scholarly references and cited evidence"
prompt = """
Activate the cite-checker skill and follow its workflow for this request.
Requested input and options: {{args}}
"""
'''


def files():
    return [
        ROOT / "SKILL.md",
        *sorted((ROOT / "scripts").glob("*.py")),
        *sorted((ROOT / "references").glob("*.md")),
    ]


def install(base, hosts):
    targets = list(dict.fromkeys(base / HOST_DIRS[h] / "skills" / "cite-checker" for h in hosts))
    command = base / ".gemini" / "commands" / "cite-checker.toml" if "gemini" in hosts else None
    package = files()
    for target in targets:
        if target.exists() and (
            not target.is_dir()
            or any(
                not (dst := target / src.relative_to(ROOT)).is_file()
                or dst.read_bytes() != src.read_bytes()
                for src in package
            )
        ):
            raise ValueError(
                f"{target} already holds a different package. Move it aside before installing."
            )
    if (
        command is not None
        and command.exists()
        and (not command.is_file() or command.read_text(encoding="utf-8") != GEMINI_COMMAND)
    ):
        raise ValueError(
            f"{command} already holds a different command. Move it aside before installing."
        )
    for target in targets:
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=target.parent) as scratch:
            staging = Path(scratch) / "cite-checker"
            for src in package:
                dst = staging / src.relative_to(ROOT)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            staging.rename(target)
    if command is not None and not command.exists():
        command.parent.mkdir(parents=True, exist_ok=True)
        with command.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(GEMINI_COMMAND)
    return [*targets, *([command] if command else [])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", choices=tuple(HOST_DIRS), nargs="+", default=list(HOST_DIRS))
    parser.add_argument(
        "--project", type=Path, help="install project-local; default is the user home"
    )
    args = parser.parse_args()
    try:
        for path in install(args.project.resolve() if args.project else Path.home(), args.hosts):
            print(path)
    except (OSError, ValueError) as error:
        parser.exit(2, f"cite-checker install: {error}\n")


if __name__ == "__main__":
    main()
