"""Herramientas de imagen: seleccionar/mover/redimensionar/borrar y añadir."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import QPoint, QPointF, QRectF, QSettings, Qt
from PySide6.QtGui import QAction, QColor, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsItem,
    QMenu,
    QStyleOptionGraphicsItem,
    QWidget,
)

from xpdf.core import images as core_images
from xpdf.core.commands import SnapshotCommand
from xpdf.core.document import Document
from xpdf.core.images import ImageInfo, Rect
from xpdf.ui.page_view import PageView, ViewTool
from xpdf.ui.qt_image import to_qpixmap

log = logging.getLogger(__name__)

ACCENT = QColor(0, 120, 215)
BLOCKED = QColor(200, 60, 60)
HANDLE_PX = 10.0  # tamaño de los tiradores en pantalla
GRAB_PX = 9.0  # distancia (px) para agarrar un tirador
MIN_SIZE = 8.0  # puntos
IMAGE_FILTER = "Imágenes (*.png *.jpg *.jpeg)"


class ImageToolHost(Protocol):
    view: PageView
    doc: Document | None

    def run_doc_command(self, cmd: SnapshotCommand[Any], error_title: str) -> bool:
        """Ejecuta y apila el comando; muestra el error y devuelve False si falla."""
        ...
    def show_status(self, message: str, timeout_ms: int = 4000) -> None: ...
    def image_selected(self, info: ImageInfo | None) -> None:
        """Avisa a la barra de imagen de la selección actual."""
        ...


# --------------------------------------------------------------- dibujo
class TransformBox(QGraphicsItem):
    """Marco con tiradores en las esquinas y, opcionalmente, una vista previa."""

    def __init__(self, parent: QGraphicsItem, color: QColor = ACCENT) -> None:
        super().__init__(parent)
        self.rect = QRectF()
        self.pixmap: QPixmap | None = None
        self.color = color
        self.handles_visible = True
        self.dashed = False
        self.setZValue(20)

    def set_rect(self, r: QRectF) -> None:
        self.prepareGeometryChange()
        self.rect = QRectF(r)
        self.update()

    def boundingRect(self) -> QRectF:
        # margen generoso: los tiradores tienen tamaño fijo en pantalla
        return self.rect.adjusted(-40, -40, 40, 40)

    def paint(self, p: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        if self.pixmap is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.setOpacity(0.9)
            p.drawPixmap(self.rect, self.pixmap, QRectF(self.pixmap.rect()))
            p.setOpacity(1.0)
        pen = QPen(self.color, 0)  # 1 px a cualquier zoom
        pen.setStyle(Qt.PenStyle.DashLine if self.dashed else Qt.PenStyle.SolidLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(self.rect)
        if not self.handles_visible:
            return
        # Tiradores de tamaño fijo en pantalla: se dibujan en coordenadas de dispositivo.
        t = p.worldTransform()
        p.save()
        p.resetTransform()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255), 1.5))
        p.setBrush(self.color)
        for c in corners(self.rect):
            dc = t.map(c)
            p.drawRoundedRect(QRectF(dc.x() - HANDLE_PX / 2, dc.y() - HANDLE_PX / 2, HANDLE_PX, HANDLE_PX), 2, 2)
        p.restore()


def corners(r: QRectF) -> list[QPointF]:
    return [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]


def _qrect(r: Rect) -> QRectF:
    return QRectF(QPointF(r[0], r[1]), QPointF(r[2], r[3]))


def _tuple(r: QRectF) -> Rect:
    return (r.left(), r.top(), r.right(), r.bottom())


# ------------------------------------------------------ arrastre compartido
class _Drag:
    """Mover o redimensionar un rectángulo con el ratón (coordenadas de página)."""

    def __init__(self, mode: str, corner: int, start: QPointF, rect: QRectF) -> None:
        self.mode = mode  # "move" | "resize"
        self.corner = corner
        self.start = start
        self.rect0 = QRectF(rect)

    def apply(self, pos: QPointF, keep_ratio: bool) -> QRectF:
        d = pos - self.start
        r = QRectF(self.rect0)
        if self.mode == "move":
            r.translate(d)
            return r
        # Redimensionar desde la esquina opuesta, que queda fija.
        anchor = corners(r)[(self.corner + 2) % 4]
        moving = corners(r)[self.corner] + d
        w = abs(moving.x() - anchor.x())
        h = abs(moving.y() - anchor.y())
        if keep_ratio and self.rect0.width() > 0 and self.rect0.height() > 0:
            ratio = self.rect0.width() / self.rect0.height()
            if w / max(h, 1e-6) > ratio:
                h = w / ratio
            else:
                w = h * ratio
        w, h = max(w, MIN_SIZE), max(h, MIN_SIZE)
        sx = 1 if moving.x() >= anchor.x() else -1
        sy = 1 if moving.y() >= anchor.y() else -1
        other = QPointF(anchor.x() + sx * w, anchor.y() + sy * h)
        return QRectF(anchor, other).normalized()


def _hit_handle(view: PageView, page: int, rect: QRectF, event: QMouseEvent) -> int:
    """Índice de la esquina bajo el ratón (-1 si ninguna)."""
    pos = event.position()
    for i, c in enumerate(corners(rect)):
        v = view.to_viewport(page, c)
        if abs(v.x() - pos.x()) <= GRAB_PX and abs(v.y() - pos.y()) <= GRAB_PX:
            return i
    return -1


_RESIZE_CURSORS = [
    Qt.CursorShape.SizeFDiagCursor,
    Qt.CursorShape.SizeBDiagCursor,
    Qt.CursorShape.SizeFDiagCursor,
    Qt.CursorShape.SizeBDiagCursor,
]


# ---------------------------------------------------------- seleccionar
class ImageSelectTool(ViewTool):
    """Seleccionar imágenes: mover, redimensionar, reemplazar y borrar."""

    def __init__(self, host: ImageToolHost) -> None:
        self.host = host
        self.page = -1
        self.info: ImageInfo | None = None
        self.box: TransformBox | None = None
        self.hover: TransformBox | None = None
        self.drag: _Drag | None = None
        self._cursor = Qt.CursorShape.ArrowCursor

    def cursor(self) -> Qt.CursorShape:
        return self._cursor

    def deactivate(self) -> None:
        self.clear()

    def reset(self) -> None:
        """El documento cambió por fuera (deshacer, otra herramienta...)."""
        self.clear()

    # -------------------------------------------------------------- estado
    def _remove(self, item: QGraphicsItem | None) -> None:
        if item is not None and item.scene() is not None:
            item.scene().removeItem(item)

    def clear(self) -> None:
        self._end_preview()
        self._remove(self.box)
        self._remove(self.hover)
        self.box = self.hover = None
        if self.info is not None:
            self.host.image_selected(None)
        self.info = None
        self.drag = None

    def select(self, page: int, info: ImageInfo) -> None:
        self.clear()
        item = self.host.view.page_item(page)
        if item is None:
            return
        self.page, self.info = page, info
        self.box = TransformBox(item)
        self.box.set_rect(_qrect(info.bbox))
        self.host.image_selected(info)
        self.host.show_status("Imagen seleccionada · arrastra para mover · esquinas para cambiar el tamaño (Mayús: libre) · Supr borra", 0)

    def _reselect_at(self, page: int, bbox: Rect) -> None:
        """Vuelve a seleccionar la imagen que ocupa `bbox` (su número puede cambiar
        al cambiar el orden de capas)."""
        doc = self.host.doc
        if doc is None:
            return
        best, dist = None, 1e9
        for info in core_images.images_on(doc, page):
            d = sum(abs(a - b) for a, b in zip(info.bbox, bbox))
            if d < dist:
                best, dist = info, d
        if best is not None and dist < 4:
            self.select(page, best)
        else:
            self.clear()

    def _reselect(self, number: int) -> None:
        doc = self.host.doc
        if doc is None:
            return
        for info in core_images.images_on(doc, self.page):
            if info.number == number:
                self.select(self.page, info)
                return
        self.clear()

    # --------------------------------------------------------------- ratón
    def mouse_move(self, page: int | None, pos: QPointF | None, event: QMouseEvent) -> bool:
        doc = self.host.doc
        if self.drag is not None and self.box is not None and pos is not None:
            keep = not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.box.set_rect(self.drag.apply(pos, keep))
            return True
        self._cursor = Qt.CursorShape.ArrowCursor
        if self.box is not None and page == self.page and pos is not None:
            h = _hit_handle(self.host.view, page, self.box.rect, event)
            if h >= 0:
                self._cursor = _RESIZE_CURSORS[h]
                return False
            if self.box.rect.contains(pos):
                self._cursor = Qt.CursorShape.SizeAllCursor
                return False
        # resaltado al pasar por encima
        self._remove(self.hover)
        self.hover = None
        if doc is None or page is None or pos is None:
            return False
        info = core_images.image_under(doc, page, pos.x(), pos.y())
        if info is not None and (self.info is None or info.number != self.info.number or page != self.page):
            item = self.host.view.page_item(page)
            if item is not None:
                self.hover = TransformBox(item, BLOCKED if info.blocked_reason else ACCENT)
                self.hover.handles_visible = False
                self.hover.dashed = True
                self.hover.set_rect(_qrect(info.bbox))
                self._cursor = Qt.CursorShape.PointingHandCursor
        return False

    def mouse_press(self, page: int, pos: QPointF, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        self.host.view.setFocus()
        doc = self.host.doc
        if doc is None or page < 0:
            self.clear()
            return False
        if self.box is not None and page == self.page:
            h = _hit_handle(self.host.view, page, self.box.rect, event)
            if h >= 0 or self.box.rect.contains(pos):
                self.drag = _Drag("resize" if h >= 0 else "move", h, pos, self.box.rect)
                self._begin_preview()
                return True
        info = core_images.image_under(doc, page, pos.x(), pos.y())
        if info is None:
            self.clear()
            return False
        if info.blocked_reason:
            self.clear()
            self.host.show_status(info.blocked_reason, 6000)
            return True
        self.select(page, info)
        self.drag = _Drag("move", -1, pos, self.box.rect if self.box else _qrect(info.bbox))
        self._begin_preview()
        return True

    def mouse_release(self, page: int | None, pos: QPointF | None, event: QMouseEvent) -> bool:
        if self.drag is None or self.box is None or self.info is None:
            return False
        new = self.box.rect
        old = _qrect(self.info.bbox)
        self.drag = None
        self._end_preview()
        moved = any(abs(a - b) > 0.3 for a, b in zip(_tuple(new), _tuple(old)))
        if not moved or self.host.doc is None:
            self.box.set_rect(old)
            return True
        number = self.info.number
        cmd = core_images.move_command(self.host.doc, self.page, number, _tuple(new))
        self.host.run_doc_command(cmd, "No se pudo mover la imagen")
        self._reselect(number)
        return True

    # ------------------------------------------------------------ teclado
    def key_press(self, event: QKeyEvent) -> bool:
        if self.info is None:
            return False
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete()
            return True
        if event.key() == Qt.Key.Key_Escape:
            self.clear()
            self.host.show_status("", 1)
            return True
        return False

    def context_menu(self, page: int, pos: QPointF, global_pos: QPoint) -> bool:
        doc = self.host.doc
        if doc is None:
            return False
        info = core_images.image_under(doc, page, pos.x(), pos.y())
        if info is None:
            return False
        if info.blocked_reason:
            self.host.show_status(info.blocked_reason, 6000)
            return True
        self.select(page, info)
        menu = QMenu(self.host.view)
        front = QAction("Traer al frente", menu)
        front.triggered.connect(self.to_front)
        behind = QAction("Enviar detrás del texto", menu)
        behind.triggered.connect(self.behind_text)
        rep = QAction("Reemplazar imagen…", menu)
        rep.triggered.connect(self.replace)
        dele = QAction("Borrar imagen", menu)
        dele.triggered.connect(self.delete)
        menu.addActions([front, behind])
        menu.addSeparator()
        menu.addActions([rep, dele])
        menu.exec(global_pos)
        return True

    # ----------------------------------------------------------- acciones
    def delete(self) -> None:
        if self.info is None or self.host.doc is None:
            return
        cmd = core_images.delete_command(self.host.doc, self.page, self.info.number)
        self.clear()
        self.host.run_doc_command(cmd, "No se pudo borrar la imagen")

    def set_opacity(self, alpha: float) -> None:
        if self.info is None or self.host.doc is None or abs(alpha - self.info.opacity) < 0.01:
            return
        page, number = self.page, self.info.number
        cmd = core_images.opacity_command(self.host.doc, page, number, alpha)
        if self.host.run_doc_command(cmd, "No se pudo cambiar la opacidad"):
            self.page = page
            self._reselect(number)

    def to_front(self) -> None:
        if self.info is None or self.host.doc is None:
            return
        page, bbox = self.page, self.info.bbox
        cmd = core_images.front_command(self.host.doc, page, self.info.number)
        if self.host.run_doc_command(cmd, "No se pudo traer la imagen al frente"):
            self.page = page
            self._reselect_at(page, bbox)
            self.host.show_status("Imagen al frente", 3000)

    def behind_text(self) -> None:
        if self.info is None or self.host.doc is None:
            return
        page, bbox = self.page, self.info.bbox
        cmd = core_images.behind_text_command(self.host.doc, page, self.info.number)
        if self.host.run_doc_command(cmd, "No se pudo enviar la imagen detrás del texto"):
            self.page = page
            self._reselect_at(page, bbox)
            self.host.show_status(
                "Imagen detrás del texto" if cmd.result else "La imagen ya estaba detrás del texto", 3000
            )

    def replace(self) -> None:
        if self.info is None or self.host.doc is None:
            return
        path = choose_image(self.host.view, "Reemplazar imagen")
        if path is None:
            return
        page, number = self.page, self.info.number
        cmd = core_images.replace_command(self.host.doc, page, number, path)
        self.clear()
        self.host.run_doc_command(cmd, "No se pudo reemplazar la imagen")

    # ------------------------------------------------------- vista previa
    def _begin_preview(self) -> None:
        """Durante el arrastre: página sin la imagen y la imagen siguiendo al ratón."""
        doc, info, box = self.host.doc, self.info, self.box
        if doc is None or info is None or box is None:
            return
        dpr = self.host.view.devicePixelRatioF()
        scale = self.host.view.zoom * dpr
        try:
            bg = core_images.preview_without(doc, self.page, info.number, scale)
            img = core_images.preview_image(doc, self.page, info.number, scale)
        except Exception:
            log.exception("No se pudo preparar la vista previa de la imagen")
            return
        self.host.view.set_page_override(self.page, to_qpixmap(bg, dpr))
        box.pixmap = to_qpixmap(img, dpr)
        box.update()

    def _end_preview(self) -> None:
        if self.page >= 0:
            self.host.view.set_page_override(self.page, None)
        if self.box is not None:
            self.box.pixmap = None
            self.box.update()


# --------------------------------------------------------------- añadir
class AddImageTool(ViewTool):
    """Clic en la página → elegir imagen → colocarla con el ratón → Intro confirma."""

    def __init__(self, host: ImageToolHost) -> None:
        self.host = host
        self.page = -1
        self.path: Path | None = None
        self.box: TransformBox | None = None
        self.drag: _Drag | None = None
        self._cursor = Qt.CursorShape.CrossCursor

    def cursor(self) -> Qt.CursorShape:
        return self._cursor

    def deactivate(self) -> None:
        self.cancel()

    def reset(self) -> None:
        self.cancel()

    def cancel(self) -> None:
        if self.box is not None and self.box.scene() is not None:
            self.box.scene().removeItem(self.box)
        self.box, self.path, self.drag = None, None, None
        self.host.show_status("", 1)

    def mouse_move(self, page: int | None, pos: QPointF | None, event: QMouseEvent) -> bool:
        if self.drag is not None and self.box is not None and pos is not None:
            keep = not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.box.set_rect(self.drag.apply(pos, keep))
            return True
        self._cursor = Qt.CursorShape.CrossCursor
        if self.box is not None and page == self.page and pos is not None:
            h = _hit_handle(self.host.view, page, self.box.rect, event)
            if h >= 0:
                self._cursor = _RESIZE_CURSORS[h]
            elif self.box.rect.contains(pos):
                self._cursor = Qt.CursorShape.SizeAllCursor
        return False

    def mouse_press(self, page: int, pos: QPointF, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton or self.host.doc is None:
            return False
        self.host.view.setFocus()
        if self.box is not None:
            if page == self.page:
                h = _hit_handle(self.host.view, page, self.box.rect, event)
                if h >= 0 or self.box.rect.contains(pos):
                    self.drag = _Drag("resize" if h >= 0 else "move", h, pos, self.box.rect)
                    return True
            self.confirm()  # clic fuera: se coloca
            return True
        if page < 0:
            return False
        path = choose_image(self.host.view, "Añadir imagen")
        if path is None:
            return True
        self._start(page, pos, path)
        return True

    def mouse_release(self, page: int | None, pos: QPointF | None, event: QMouseEvent) -> bool:
        if self.drag is None:
            return False
        self.drag = None
        return True

    def key_press(self, event: QKeyEvent) -> bool:
        if self.box is None:
            return False
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.confirm()
            return True
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
            return True
        return False

    def _start(self, page: int, pos: QPointF, path: Path) -> None:
        item = self.host.view.page_item(page)
        pm = QPixmap(str(path))
        if item is None or pm.isNull():
            self.host.show_status(f"No se pudo leer la imagen «{path.name}».", 6000)
            return
        # Tamaño inicial: el natural a 96 ppp, como mucho el 40 % del ancho de la página.
        page_w = item.boundingRect().width()
        w = pm.width() * 72 / 96
        h = pm.height() * 72 / 96
        if w > 0.4 * page_w:
            h *= 0.4 * page_w / w
            w = 0.4 * page_w
        r = QRectF(pos.x() - w / 2, pos.y() - h / 2, w, h)
        self.page, self.path = page, path
        self.box = TransformBox(item)
        self.box.pixmap = pm
        self.box.set_rect(r)
        self.host.show_status("Coloca la imagen · arrastra o usa las esquinas · Intro o clic fuera para confirmar · Esc cancela", 0)

    def confirm(self) -> None:
        if self.box is None or self.path is None or self.host.doc is None:
            self.cancel()
            return
        rect, page, path = _tuple(self.box.rect), self.page, self.path
        self.cancel()
        cmd = core_images.add_command(self.host.doc, page, rect, path)
        if self.host.run_doc_command(cmd, "No se pudo añadir la imagen"):
            self.host.show_status("Imagen añadida · con «Seleccionar» puedes moverla o borrarla", 5000)


def choose_image(parent: QWidget, title: str) -> Path | None:
    settings = QSettings()
    start = str(settings.value("paths/last_image_dir", str(Path.home())))
    name, _ = QFileDialog.getOpenFileName(parent, title, start, IMAGE_FILTER)
    if not name:
        return None
    settings.setValue("paths/last_image_dir", str(Path(name).parent))
    return Path(name)
