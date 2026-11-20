"""Prepara una versión nueva: sube el número, pasa los tests, hace commit y etiqueta.

    uv run python tools/release.py 0.3.1

Después, para publicarla (GitHub Actions compila y publica):

    git push && git push origin v0.3.1
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "src" / "xpdf" / "__init__.py"


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, cwd=ROOT, check=True, text=True, capture_output=True).stdout


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[1]):
        print(__doc__)
        return 2
    version = sys.argv[1]
    if sh("git", "status", "--porcelain").strip():
        print("Hay cambios sin commit: haz commit antes de preparar una versión.")
        return 1
    text = INIT.read_text(encoding="utf-8")
    new = re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', text)
    INIT.write_text(new, encoding="utf-8")
    print("Pasando los tests...")
    subprocess.run(["uv", "run", "pytest", "-q"], cwd=ROOT, check=True)
    sh("git", "commit", "-am", f"xpdf {version}")
    sh("git", "tag", "-a", f"v{version}", "-m", f"xpdf {version}")
    print(f"\nVersión {version} preparada. Para publicarla:\n\n    git push && git push origin v{version}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
