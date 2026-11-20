"""Iconos SVG que se tiñen con el color de texto de la paleta (tema claro/oscuro)."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

from xpdf.paths import assets_dir


@lru_cache(maxsize=None)
def _svg_source(name: str) -> str:
    return (assets_dir() / "icons" / f"{name}.svg").read_text(encoding="utf-8")


def _render(svg: str, size: int, dpr: float) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    return pm


def _faded(pm: QPixmap, opacity: float = 0.3) -> QPixmap:
    """Versión atenuada del icono entero. (El color «desactivado» de la paleta lleva
    alfa en macOS y aplicarlo por trazo oscurece los cruces.)"""
    out = QPixmap(pm.size())
    out.setDevicePixelRatio(pm.devicePixelRatio())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setOpacity(opacity)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


def icon(name: str) -> QIcon:
    pal = QGuiApplication.palette()
    normal = pal.color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText).name()
    src = _svg_source(name)
    dpr = 2.0  # suficiente para pantallas HiDPI; Qt reduce si hace falta
    ic = QIcon()
    for size in (16, 20, 24, 32):
        pm = _render(src.replace("currentColor", normal), size, dpr)
        ic.addPixmap(pm, QIcon.Mode.Normal)
        ic.addPixmap(_faded(pm), QIcon.Mode.Disabled)
    return ic
