"""Configura el repositorio público de GitHub (usuario/nombre) en todo el proyecto.

    uv run python tools/set_repo.py mi-usuario/xpdf

Sustituye el marcador en el código (actualizador), los instaladores de una línea
y el README.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["src/xpdf/__init__.py", "scripts/install.ps1", "scripts/install.sh", "README.md"]
PLACEHOLDER = "TU-USUARIO/xpdf"


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", sys.argv[1]):
        print(__doc__)
        return 2
    repo = sys.argv[1]
    for rel in FILES:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        n = text.count(PLACEHOLDER)
        path.write_text(text.replace(PLACEHOLDER, repo), encoding="utf-8")
        print(f"{rel}: {n} sustitución(es)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
