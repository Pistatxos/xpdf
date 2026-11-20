"""Conversión de los mapas de bits de core a tipos Qt."""

from __future__ import annotations

from PySide6.QtGui import QImage, QPixmap

from xpdf.core.document import RenderedPage


def to_qimage(r: RenderedPage) -> QImage:
    img = QImage(r.samples, r.width, r.height, r.stride, QImage.Format.Format_RGB888)
    return img.copy()  # desacoplar del buffer de bytes


def to_qpixmap(r: RenderedPage, dpr: float = 1.0) -> QPixmap:
    pm = QPixmap.fromImage(to_qimage(r))
    pm.setDevicePixelRatio(dpr)
    return pm
