"""Genera el icono de la app (assets/icons/app.png, .ico y .icns).

Uso: uv run python tools/make_icon.py
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
)

OUT = Path(__file__).resolve().parents[1] / "assets" / "icons"


#: Tipografía del logotipo (Inter Black, SIL OFL; ver tools/fonts/LICENSE-Inter.txt).
#: Solo se usa para generar el icono: no se incluye en la aplicación.
FONT = Path(__file__).resolve().parent / "fonts" / "Inter-Black.ttf"
BLUE1, BLUE2, ORANGE = QColor("#3b82f6"), QColor("#4338ca"), QColor("#f97316")
#: A este tamaño (px lógicos) o menos solo se dibuja la «x»: «xpdf» no se leería.
SMALL = 24
#: Hasta este tamaño el nombre se dibuja un poco más grande para que se lea.
MEDIUM = 64
_family: str | None = None


def _font(px: int) -> QFont:
    global _family
    if _family is None:
        fid = QFontDatabase.addApplicationFont(str(FONT))
        if fid < 0:
            raise SystemExit(f"No se pudo cargar {FONT}")
        _family = QFontDatabase.applicationFontFamilies(fid)[0]
    f = QFont(_family)
    f.setPixelSize(px)
    return f


def draw(size: int, small: bool | None = None, logical: int | None = None) -> QImage:
    """Logo: «xpdf» con la x naranja sobre azul; en tamaños mínimos, solo la x.

    `logical` es el tamaño en que se mostrará (un @2x de 32 se ve a 32): decide
    el dibujo; `size` son los píxeles de la imagen.
    """
    logical = logical or size
    if small is None:
        small = logical <= SMALL
    scale = 1.12 if logical <= MEDIUM else 1.0
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.scale(size / 1024.0, size / 1024.0)

    bg = QRectF(100, 100, 824, 824)
    grad = QLinearGradient(bg.topLeft(), bg.bottomRight())
    grad.setColorAt(0, BLUE1)
    grad.setColorAt(1, BLUE2)
    path = QPainterPath()
    path.addRoundedRect(bg, 185, 185)
    p.fillPath(path, QBrush(grad))

    if small:
        p.setFont(_font(680))
        p.setPen(QColor("white"))
        p.drawText(QRectF(100, 60, 824, 824), Qt.AlignmentFlag.AlignCenter, "x")
    else:
        f = _font(round(270 * scale))
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, -8 * scale)
        p.setFont(f)
        fm = QFontMetricsF(f)
        wx, wp = fm.horizontalAdvance("x"), fm.horizontalAdvance("pdf")
        x0 = 512 - (wx + wp) / 2
        baseline = 512 + fm.capHeight() / 2
        p.setPen(ORANGE)
        p.drawText(QPointF(x0, baseline), "x")
        p.setPen(QColor("white"))
        p.drawText(QPointF(x0 + wx, baseline), "pdf")
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data().data())


def write_ico(path: Path, sizes: tuple[int, ...]) -> None:
    """ICO con varias resoluciones (entradas PNG, válidas desde Windows Vista)."""
    images = [(sz, png_bytes(draw(sz))) for sz in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for sz, data in images:
        dim = 0 if sz >= 256 else sz  # 0 significa 256
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    path.write_bytes(header + entries + blobs)


def write_icns(path: Path) -> bool:
    """`.icns` con iconutil (solo macOS). Devuelve False si no está disponible."""
    if shutil.which("iconutil") is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "app.iconset"
        iconset.mkdir()
        for sz in (16, 32, 128, 256, 512):
            # el @2x se ve al mismo tamaño lógico: mismo dibujo, más píxeles
            draw(sz).save(str(iconset / f"icon_{sz}x{sz}.png"))
            draw(sz * 2, logical=sz).save(str(iconset / f"icon_{sz}x{sz}@2x.png"))
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(path)], check=True)
    return True


def main() -> int:
    QGuiApplication(sys.argv[:1])
    OUT.mkdir(parents=True, exist_ok=True)
    draw(1024).save(str(OUT / "app.png"))
    draw(256, logical=16).save(str(OUT / "app-small.png"))  # icono de ventana a 16-24 px: solo la x
    draw(256, logical=32).save(str(OUT / "app-medium.png"))  # 32-64 px: «xpdf» algo mayor
    write_ico(OUT / "app.ico", (16, 24, 32, 48, 64, 128, 256))
    if not write_icns(OUT / "app.icns"):
        print("Aviso: sin iconutil (no es macOS); app.icns no se ha regenerado")
    print("Iconos escritos en", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
