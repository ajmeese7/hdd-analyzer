"""Print the CHANGELOG.md section for one version, for `gh release create --notes-file`.

Usage: python scripts/release-notes.py X.Y.Z > notes.md
"""

import re
import sys
from pathlib import Path


def section(changelog: str, version: str) -> str:
    match = re.search(rf"^## {re.escape(version)} - [\d-]+\n\n(.*?)(?=^## |\Z)", changelog, re.S | re.M)
    if match is None:
        raise SystemExit(f"no '## {version} - YYYY-MM-DD' section in CHANGELOG.md")
    return match.group(1).strip() + "\n"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sys.stdout.write(section(Path("CHANGELOG.md").read_text(encoding="utf-8"), sys.argv[1]))
