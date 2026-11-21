"""Herramienta «Firmar»: dibujar el recuadro donde irá la firma digital."""

from __future__ import annotations

from typing import Protocol

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent

from xpdf.ui.page_view import PageView, ViewTool
from xpdf.ui.tools.image_tools import TransformBox

SIGN_COLOR = QColor(0, 140, 80)
MIN_W, MIN_H = 60.0, 24.0  # puntos: más pequeño no se lee
DEFAULT_W, DEFAULT_H = 200.0, 60.0  # tamaño si solo se hace clic


class SignToolHost(Protocol):
    view: PageView

    def sign_area(self, page: int, rect: tuple[float, float, float, float]) -> None:
        """El usuario ha marcado dónde firmar (coordenadas de página)."""
        ...
    def show_status(self, message: str, timeout_ms: int = 4000) -> None: ...


class SignAreaTool(ViewTool):
    """Arrastra para dibujar el recuadro de la firma (o haz clic para uno estándar)."""

    def __init__(self, host: SignToolHost) -> None:
        self.host = host
        self.page = -1
        self.start: QPointF | None = None
        self.box: TransformBox | None = None

    def cursor(self) -> Qt.CursorShape:
        return Qt.CursorShape.CrossCursor

    def activate(self) -> None:
        self.host.show_status(
            "Dibuja el recuadro de la firma arrastrando sobre la página (o haz clic) · Esc cancela", 0
        )

    def deactivate(self) -> None:
        self._clear()
        self.host.show_status("", 1)

    def _clear(self) -> None:
        if self.box is not None and self.box.scene() is not None:
            self.box.scene().removeItem(self.box)
        self.box, self.start = None, None

    def mouse_press(self, page: int, pos: QPointF, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton or page < 0:
            return False
        item = self.host.view.page_item(page)
        if item is None:
            return False
        self._clear()
        self.page, self.start = page, pos
        self.box = TransformBox(item, SIGN_COLOR)
        self.box.handles_visible = False
        self.box.dashed = True
        self.box.set_rect(QRectF(pos, pos))
        return True

    def mouse_move(self, page: int | None, pos: QPointF | None, event: QMouseEvent) -> bool:
        if self.start is None or self.box is None or pos is None:
            return False
        self.box.set_rect(QRectF(self.start, pos).normalized())
        return True

    def mouse_release(self, page: int | None, pos: QPointF | None, event: QMouseEvent) -> bool:
        if self.start is None or self.box is None:
            return False
        r = self.box.rect
        item = self.host.view.page_item(self.page)
        if r.width() < 6 and r.height() < 6:  # un clic: recuadro estándar centrado
            r = QRectF(self.start.x() - DEFAULT_W / 2, self.start.y() - DEFAULT_H / 2, DEFAULT_W, DEFAULT_H)
        else:
            r.setWidth(max(r.width(), MIN_W))
            r.setHeight(max(r.height(), MIN_H))
        if item is not None:  # dentro de la página
            bounds = item.boundingRect()
            r.moveLeft(min(max(r.left(), 0.0), bounds.width() - r.width()))
            r.moveTop(min(max(r.top(), 0.0), bounds.height() - r.height()))
        self.box.set_rect(r)
        page = self.page
        self.start = None
        self.host.sign_area(page, (r.left(), r.top(), r.right(), r.bottom()))
        self._clear()
        return True

    def key_press(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Escape:
            self._clear()
            self.host.show_status("", 1)
            return True
        return False
