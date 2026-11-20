"""Herramientas «Editar texto» y «Añadir texto»."""

from __future__ import annotations

import logging
from typing import Protocol

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import QGraphicsRectItem

from xpdf.core.commands import SnapshotCommand
from xpdf.core.editing import NewTextStyle, ParagraphInfo, Span, TextEditing, TextOutcome
from xpdf.core.errors import XpdfError
from xpdf.ui.page_view import PageView, ViewTool
from xpdf.ui.qt_image import to_qpixmap
from xpdf.ui.text_editor import TextEditorOverlay

log = logging.getLogger(__name__)

ACCENT = QColor(0, 120, 215)
BLOCKED = QColor(200, 60, 60)


class TextToolHost(Protocol):
    """Lo que las herramientas necesitan de la ventana principal."""

    view: PageView
    editing: TextEditing | None

    def run_command(self, cmd: SnapshotCommand[TextOutcome]) -> TextOutcome | None:
        """Ejecuta y apila el comando. Muestra el error y devuelve None si falla."""
        ...

    def show_status(self, message: str, timeout_ms: int = 4000) -> None: ...

    def new_text_style(self) -> NewTextStyle: ...

    def editor_changed(self, overlay: TextEditorOverlay | None, mode: str) -> None:
        """Avisa a la barra de formato de qué editor está activo."""
        ...


class _Highlight:
    """Rectángulo de resaltado al pasar el ratón por un párrafo."""

    def __init__(self, view: PageView) -> None:
        self.view = view
        self.item: QGraphicsRectItem | None = None
        self.key: tuple[int, int] | None = None

    def show(self, page: int, para_id: int, bbox: tuple[float, float, float, float], blocked: bool) -> None:
        if self.key == (page, para_id) and self.item is not None:
            return
        self.clear()
        parent = self.view.page_item(page)
        if parent is None:
            return
        x0, y0, x1, y1 = bbox
        item = QGraphicsRectItem(QRectF(x0 - 2, y0 - 1, x1 - x0 + 4, y1 - y0 + 2), parent)
        color = BLOCKED if blocked else ACCENT
        pen = QPen(color, 0)  # cosmético: 1 px a cualquier zoom
        if blocked:
            pen.setStyle(Qt.PenStyle.DashLine)
        item.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(22)
        item.setBrush(QBrush(fill))
        item.setZValue(5)
        self.item, self.key = item, (page, para_id)

    def clear(self) -> None:
        if self.item is not None:
            scene = self.item.scene()
            if scene is not None:
                scene.removeItem(self.item)
        self.item, self.key = None, None


class EditTextTool(ViewTool):
    """Clic en un párrafo: se edita en su sitio, con su fuente, tamaño y color."""

    def __init__(self, host: TextToolHost) -> None:
        self.host = host
        self.highlight = _Highlight(host.view)
        self.overlay: TextEditorOverlay | None = None
        self.para: ParagraphInfo | None = None

    def cursor(self) -> Qt.CursorShape:
        return Qt.CursorShape.IBeamCursor

    def deactivate(self) -> None:
        self.commit()
        self.highlight.clear()

    # ---------------------------------------------------------------- ratón
    def mouse_move(self, page: int | None, pos: QPointF | None, event: QMouseEvent) -> bool:
        ed = self.host.editing
        if ed is None or page is None or pos is None:
            self.highlight.clear()
            return False
        hit = ed.hit(page, pos.x(), pos.y())
        if hit is None or (self.para is not None and (page, hit[0]) == (self.para.page, self.para.id)):
            self.highlight.clear()
            return False
        self.highlight.show(page, hit[0], hit[1], hit[2] is not None)
        return False

    def mouse_press(self, page: int, pos: QPointF, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        ed = self.host.editing
        if ed is None:
            return False
        if self.overlay is not None:
            self.commit()
        if page < 0:
            return True
        hit = ed.hit(page, pos.x(), pos.y())  # tras confirmar, ids y cajas pueden cambiar
        if hit is None:
            return True
        if hit[2] is not None:
            self.host.show_status(hit[2], 6000)
            return True
        self.begin(ed.paragraph(page, hit[0]))
        return True

    def key_press(self, event: QKeyEvent) -> bool:
        return False

    # -------------------------------------------------------------- edición
    def begin(self, para: ParagraphInfo) -> None:
        ed = self.host.editing
        item = self.host.view.page_item(para.page)
        if ed is None or item is None:
            return
        self.highlight.clear()
        self.para = para
        # Fondo sin el texto original, al zoom actual.
        scale = self.host.view.zoom * self.host.view.devicePixelRatioF()
        try:
            bg = ed.render_without(para.page, para.id, scale)
            self.host.view.set_page_override(para.page, to_qpixmap(bg, self.host.view.devicePixelRatioF()))
        except Exception:
            log.exception("No se pudo preparar el fondo del editor")
        ov = TextEditorOverlay(item)
        ov.edit.committed.connect(self.commit)
        ov.edit.cancelled.connect(self.cancel)
        ov.load_paragraph(para)
        self.overlay = ov
        ov.focus()
        self.host.editor_changed(ov, "edit")
        fonts = sorted({s.font_name for s in para.styles})
        self.host.show_status("Editando: " + ", ".join(fonts) + "  ·  Esc cancela, Ctrl+Intro confirma", 0)

    def _close(self) -> None:
        if self.overlay is not None:
            scene = self.overlay.scene()
            if scene is not None:
                scene.removeItem(self.overlay)
            self.overlay.deleteLater()
        if self.para is not None:
            self.host.view.set_page_override(self.para.page, None)
        self.overlay, self.para = None, None
        self.host.editor_changed(None, "edit")
        self.host.show_status("", 1)

    def cancel(self) -> None:
        self._close()

    def commit(self) -> None:
        ov, para, ed = self.overlay, self.para, self.host.editing
        if ov is None or para is None or ed is None:
            return
        spans = ov.spans()
        align = ov.align_override if ov.align_override != para.align else None
        if align is None and ov.box_override is None and _unchanged(para, spans):
            self._close()
            return
        cmd = ed.edit_command(para.page, para.id, spans, align, ov.box_override)  # type: ignore[arg-type]
        outcome = self.host.run_command(cmd)
        if outcome is None:
            # Error (p. ej. no cabe): se deja el editor abierto para corregir.
            ov.focus()
            return
        self._close()
        if outcome.message():
            self.host.show_status(outcome.message(), 10000)


class AddTextTool(ViewTool):
    """Clic en cualquier punto de la página: escribe texto nuevo allí."""

    def __init__(self, host: TextToolHost) -> None:
        self.host = host
        self.overlay: TextEditorOverlay | None = None
        self.page = -1
        self.pos = QPointF()

    def cursor(self) -> Qt.CursorShape:
        return Qt.CursorShape.CrossCursor

    def deactivate(self) -> None:
        self.commit()

    def mouse_press(self, page: int, pos: QPointF, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton or self.host.editing is None:
            return False
        if self.overlay is not None:
            self.commit()
            return True  # el primer clic fuera confirma; el siguiente crea otro
        if page < 0:
            return True
        self.begin(page, pos)
        return True

    def begin(self, page: int, pos: QPointF) -> None:
        ed = self.host.editing
        item = self.host.view.page_item(page)
        if ed is None or item is None:
            return
        self.page, self.pos = page, pos
        ov = TextEditorOverlay(item)
        ov.edit.committed.connect(self.commit)
        ov.edit.cancelled.connect(self.cancel)
        width = item.boundingRect().width() - pos.x() - 4
        ov.start_new(pos, ed.style_preview(self.host.new_text_style()), width)
        self.overlay = ov
        ov.focus()
        self.host.editor_changed(ov, "add")
        self.host.show_status("Escribe el texto  ·  Esc cancela, clic fuera o Ctrl+Intro confirma", 0)

    def restyle(self) -> None:
        """La barra de formato cambió el estilo del texto nuevo."""
        ed = self.host.editing
        if self.overlay is not None and ed is not None:
            self.overlay.apply_style(ed.style_preview(self.host.new_text_style()))
            self.overlay.focus()

    def _close(self) -> None:
        if self.overlay is not None:
            scene = self.overlay.scene()
            if scene is not None:
                scene.removeItem(self.overlay)
            self.overlay.deleteLater()
        self.overlay = None
        self.host.editor_changed(None, "add")
        self.host.show_status("", 1)

    def cancel(self) -> None:
        self._close()

    def commit(self) -> None:
        ov, ed = self.overlay, self.host.editing
        if ov is None or ed is None:
            return
        text = ov.plain_text()
        if not text.strip():
            self._close()
            return
        try:
            box = ov.box_override
            x = box[0] if box else self.pos.x()
            width = (box[1] - box[0]) if box else None
            cmd = ed.add_command(self.page, x, self.pos.y(), text, self.host.new_text_style(), width)
        except XpdfError as err:
            self.host.show_status(str(err), 6000)
            return
        outcome = self.host.run_command(cmd)
        if outcome is None:
            ov.focus()
            return
        self._close()
        if outcome.message():
            self.host.show_status(outcome.message(), 10000)


def _unchanged(para: ParagraphInfo, spans: list[Span]) -> bool:
    """¿El usuario no tocó nada? (mismo texto y sin cambios de formato)."""
    if any(s.bold is not None or s.italic is not None or s.size is not None or s.color is not None for s in spans):
        return False
    return "".join(s.text for s in spans) == para.text
