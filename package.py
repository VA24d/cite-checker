"""Build dist/cite-checker.zip: the skill folder alone, for upload to Claude.ai or other hosts."""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL = ROOT / "skills" / "cite-checker"


def build(out=ROOT / "dist" / "cite-checker.zip"):
    out.parent.mkdir(exist_ok=True)
    files = sorted(
        p
        for p in SKILL.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            # Fixed timestamps keep the archive byte-identical across builds.
            info = zipfile.ZipInfo(str(Path("cite-checker") / path.relative_to(SKILL)))
            info.date_time = (2026, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return out, len(files)


if __name__ == "__main__":
    path, count = build()
    print(f"{path} ({count} files)")
