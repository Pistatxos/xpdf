"""Construye xpdf para el sistema actual.

    uv run python build.py

- Windows: dist/xpdf/ (PyInstaller) y dist/xpdf-<versión>-windows-x64-setup.exe
  (Inno Setup; necesita ISCC.exe, o la variable de entorno ISCC con su ruta).
- macOS:   dist/xpdf.app y dist/xpdf-<versión>-macos-arm64.zip
Además escribe dist/SHA256SUMS.txt y comprueba el resultado con --selftest.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
sys.path.insert(0, str(ROOT / "src"))
from xpdf import __version__  # noqa: E402


def run(*cmd: str) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def version_info() -> None:
    """Recurso de versión del .exe (nombre, versión, empresa...) para Windows."""
    nums = [int(x) for x in __version__.split(".")[:3]] + [0]
    t = tuple(nums[:4])
    text = f"""# Generado por build.py
VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040A04B0', [
      StringStruct('CompanyName', 'xpdf'),
      StringStruct('FileDescription', 'xpdf - editor de PDF'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'xpdf'),
      StringStruct('LegalCopyright', 'AGPL-3.0-or-later'),
      StringStruct('OriginalFilename', 'xpdf.exe'),
      StringStruct('ProductName', 'xpdf'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1034, 1200])])
  ]
)
"""
    out = ROOT / "packaging" / "windows" / "version_info.txt"
    out.write_text(text, encoding="utf-8")


def pyinstaller() -> None:
    run(
        sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "xpdf.spec"),
        "--noconfirm", "--clean", "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"),
    )


def selftest(exe: Path) -> None:
    report = Path(tempfile.gettempdir()) / "xpdf-selftest.txt"
    report.unlink(missing_ok=True)
    proc = subprocess.run([str(exe), f"--selftest={report}"], timeout=180)
    text = report.read_text(encoding="utf-8") if report.exists() else "(sin informe)"
    print(text)
    if proc.returncode != 0 or "RESULTADO: OK" not in text:
        raise SystemExit("La autocomprobación del ejecutable ha fallado")


def find_iscc() -> str:
    candidates = [
        os.environ.get("ISCC", ""),
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        shutil.which("iscc") or "",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise SystemExit("No se encuentra Inno Setup (ISCC.exe). Instálalo o define ISCC.")


def build_windows() -> list[Path]:
    version_info()
    pyinstaller()
    selftest(DIST / "xpdf" / "xpdf.exe")
    run(
        find_iscc(), f"/DAppVersion={__version__}", f"/DSourceDir={DIST / 'xpdf'}",
        f"/DOutputDir={DIST}", f"/DRootDir={ROOT}", str(ROOT / "packaging" / "windows" / "xpdf.iss"),
    )
    return [DIST / f"xpdf-{__version__}-windows-x64-setup.exe"]


def build_macos() -> list[Path]:
    pyinstaller()
    app = DIST / "xpdf.app"
    # Firma ad hoc (sin certificado): Apple Silicon no ejecuta binarios sin firma.
    run("codesign", "--force", "--deep", "--sign", "-", str(app))
    selftest(app / "Contents" / "MacOS" / "xpdf")
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    out = DIST / f"xpdf-{__version__}-macos-{arch}.zip"
    out.unlink(missing_ok=True)
    run("ditto", "-c", "-k", "--keepParent", str(app), str(out))
    return [out]


def write_sums(files: list[Path]) -> None:
    lines = []
    for f in files:
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        lines.append(f"{digest}  {f.name}")
    (DIST / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    DIST.mkdir(exist_ok=True)
    if sys.platform == "win32":
        files = build_windows()
    elif sys.platform == "darwin":
        files = build_macos()
    else:
        raise SystemExit("Solo se empaqueta para Windows y macOS.")
    write_sums(files)
    for f in files:
        print(f"Listo: {f}  ({f.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
