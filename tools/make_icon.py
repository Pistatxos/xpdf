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

from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

OUT = Path(__file__).resolve().parents[1] / "assets" / "icons"


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 1024.0
    p.scale(s, s)

    # Fondo: cuadrado redondeado (margen como los iconos de macOS) con degradado.
    bg = QRectF(100, 100, 824, 824)
    grad = QLinearGradient(bg.topLeft(), bg.bottomRight())
    grad.setColorAt(0, QColor("#3b82f6"))
    grad.setColorAt(1, QColor("#4338ca"))
    path = QPainterPath()
    path.addRoundedRect(bg, 185, 185)
    p.fillPath(path, QBrush(grad))

    # Hoja con la esquina doblada.
    x0, y0, x1, y1, fold = 300.0, 210.0, 724.0, 814.0, 130.0
    page = QPainterPath()
    page.moveTo(x0, y0)
    page.lineTo(x1 - fold, y0)
    page.lineTo(x1, y0 + fold)
    page.lineTo(x1, y1)
    page.lineTo(x0, y1)
    page.closeSubpath()
    p.fillPath(page, QColor("#ffffff"))
    corner = QPainterPath()
    corner.moveTo(x1 - fold, y0)
    corner.lineTo(x1 - fold, y0 + fold)
    corner.lineTo(x1, y0 + fold)
    corner.closeSubpath()
    p.fillPath(corner, QColor("#c7d2fe"))

    # Líneas de texto.
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#94a3b8"))
    for i, w in enumerate((330, 300, 330, 190)):
        y = 400 + i * 95
        p.drawRoundedRect(QRectF(360, y, w if i != 1 else 200, 38), 19, 19)
    # Cursor de texto (edición) tras la segunda línea.
    p.setBrush(QColor("#f97316"))
    p.drawRoundedRect(QRectF(585, 470, 30, 115), 12, 12)
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
            draw(sz).save(str(iconset / f"icon_{sz}x{sz}.png"))
            draw(sz * 2).save(str(iconset / f"icon_{sz}x{sz}@2x.png"))
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(path)], check=True)
    return True


def main() -> int:
    QGuiApplication(sys.argv[:1])
    OUT.mkdir(parents=True, exist_ok=True)
    draw(1024).save(str(OUT / "app.png"))
    write_ico(OUT / "app.ico", (16, 24, 32, 48, 64, 128, 256))
    if not write_icns(OUT / "app.icns"):
        print("Aviso: sin iconutil (no es macOS); app.icns no se ha regenerado")
    print("Iconos escritos en", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
