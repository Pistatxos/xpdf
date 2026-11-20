"""Panel lateral de miniaturas."""

from __future__ import annotations

import time

from PySide6.QtCore import QItemSelectionModel, QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QContextMenuEvent,
    QDragMoveEvent,
    QDropEvent,
    QIcon,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem, QMenu, QWidget

from xpdf.core.document import Document
from xpdf.ui.qt_image import to_qimage

THUMB_W = 120
THUMB_H = 160
#: Tiempo máximo por tanda de renderizado para no bloquear la interfaz.
BATCH_BUDGET_S = 0.025


class ThumbnailPanel(QListWidget):
    pageActivated = Signal(int)
    #: (páginas seleccionadas, posición destino 0..n) al soltar un arrastre
    pagesMoved = Signal(list, int)
    deleteRequested = Signal(list)
    #: (páginas, grados: -90 o 90)
    rotateRequested = Signal(list, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._doc: Document | None = None
        self._pending: list[int] = []
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.TopToBottom)
        self.setWrapping(False)
        self.setMovement(QListWidget.Movement.Static)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setIconSize(QSize(THUMB_W, THUMB_H))
        self.setSpacing(6)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Reordenar arrastrando. El soltar lo resuelve la ventana (comando con
        # deshacer); la lista no mueve sus elementos por su cuenta.
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._drop_target: int | None = None
        self.setMinimumWidth(THUMB_W + 40)
        self.setFrameShape(QListWidget.Shape.NoFrame)

        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._render_batch)
        self.itemClicked.connect(self._on_clicked)
        self.currentRowChanged.connect(self._on_row_changed)

    def set_document(self, doc: Document | None) -> None:
        self._timer.stop()
        self._doc = doc
        self.clear()
        self._pending = []
        if doc is None:
            return
        for i in range(doc.page_count):
            size = doc.page_size(i)
            item = QListWidgetItem(self._placeholder(size.width, size.height), str(i + 1))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setSizeHint(QSize(THUMB_W + 16, THUMB_H + 28))
            self.addItem(item)
        self._pending = list(range(doc.page_count))
        self._timer.start()

    def refresh(self) -> None:
        """Vuelve a generar las miniaturas (el documento cambió); sin parpadeo."""
        if self._doc is None:
            return
        if self.count() != self._doc.page_count:
            self.set_document(self._doc)
            return
        self._pending = list(range(self._doc.page_count))
        self._timer.start()

    def set_current_page(self, index: int) -> None:
        """Sincroniza con la vista sin re-emitir `pageActivated`."""
        if 0 <= index < self.count() and self.currentRow() != index:
            self.blockSignals(True)
            self.setCurrentRow(index)
            self.blockSignals(False)
            self.scrollToItem(self.item(index), QAbstractItemView.ScrollHint.EnsureVisible)

    def selected_pages(self) -> list[int]:
        return sorted(self.row(it) for it in self.selectedItems())

    def select_pages(self, pages: list[int]) -> None:
        self.blockSignals(True)
        self.clearSelection()
        for i in pages:
            item = self.item(i)
            if item is not None:
                item.setSelected(True)
        first = self.item(pages[0]) if pages else None
        if first is not None:
            self.setCurrentItem(first, QItemSelectionModel.SelectionFlag.NoUpdate)
            self.scrollToItem(first)
        self.blockSignals(False)

    # ------------------------------------------------------- arrastrar/soltar
    def _target_at(self, pos: QPoint) -> int:
        """Posición de destino (0..n) para un punto del área visible."""
        item = self.itemAt(pos)
        if item is None:
            if self.count() and pos.y() < self.visualItemRect(self.item(0)).top():
                return 0
            return self.count()
        rect = self.visualItemRect(item)
        row = self.row(item)
        return row + 1 if pos.y() > rect.center().y() else row

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if event.source() is not self:
            event.ignore()
            return
        self._drop_target = self._target_at(event.position().toPoint())
        self.viewport().update()
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event: object) -> None:
        self._drop_target = None
        self.viewport().update()

    def dropEvent(self, event: QDropEvent) -> None:
        target = self._target_at(event.position().toPoint())
        self._drop_target = None
        self.viewport().update()
        pages = self.selected_pages()
        # La lista no debe mover nada: se ignora la acción y se avisa a la ventana.
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        if pages:
            self.pagesMoved.emit(pages, target)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if self._drop_target is None or not self.count():
            return
        if self._drop_target < self.count():
            y = self.visualItemRect(self.item(self._drop_target)).top() - self.spacing() / 2
        else:
            y = self.visualItemRect(self.item(self.count() - 1)).bottom() + self.spacing() / 2
        p = QPainter(self.viewport())
        p.setPen(QPen(self.palette().color(QPalette.ColorRole.Highlight), 3))
        p.drawLine(8, round(y), self.viewport().width() - 8, round(y))
        p.end()

    # ------------------------------------------------------------ menú y teclas
    def _pages_for_action(self) -> list[int]:
        pages = self.selected_pages()
        if not pages and self.currentRow() >= 0:
            pages = [self.currentRow()]
        return pages

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        item = self.itemAt(event.pos())
        if item is not None and not item.isSelected():
            self.select_pages([self.row(item)])
        pages = self._pages_for_action()
        if not pages:
            return
        n = len(pages)
        label = "la página" if n == 1 else f"{n} páginas"
        menu = QMenu(self)
        left = QAction(f"Girar {label} a la izquierda", menu)
        right = QAction(f"Girar {label} a la derecha", menu)
        delete = QAction(f"Borrar {label}", menu)
        left.triggered.connect(lambda: self.rotateRequested.emit(pages, -90))
        right.triggered.connect(lambda: self.rotateRequested.emit(pages, 90))
        delete.triggered.connect(lambda: self.deleteRequested.emit(pages))
        menu.addActions([left, right])
        menu.addSeparator()
        menu.addAction(delete)
        menu.exec(event.globalPos())

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            pages = self._pages_for_action()
            if pages:
                self.deleteRequested.emit(pages)
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ interno
    def _on_clicked(self, item: QListWidgetItem) -> None:
        self.pageActivated.emit(self.row(item))

    def _on_row_changed(self, row: int) -> None:
        if row >= 0:
            self.pageActivated.emit(row)

    def _fit(self, w: float, h: float) -> tuple[int, int]:
        s = min(THUMB_W / w, THUMB_H / h)
        return max(1, round(w * s)), max(1, round(h * s))

    def _placeholder(self, w: float, h: float) -> QIcon:
        tw, th = self._fit(w, h)
        pm = QPixmap(THUMB_W, THUMB_H)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        x, y = (THUMB_W - tw) // 2, (THUMB_H - th) // 2
        p.fillRect(x, y, tw, th, Qt.GlobalColor.white)
        p.setPen(QColor(0, 0, 0, 60))
        p.drawRect(x, y, tw - 1, th - 1)
        p.end()
        return QIcon(pm)

    def _render_batch(self) -> None:
        if self._doc is None or not self._pending:
            self._timer.stop()
            return
        # Prioriza las miniaturas visibles.
        first = self.indexAt(self.viewport().rect().topLeft()).row()
        if first > 0 and first in self._pending:
            self._pending.remove(first)
            self._pending.insert(0, first)
        dpr = self.devicePixelRatioF()
        start = time.perf_counter()
        while self._pending and time.perf_counter() - start < BATCH_BUDGET_S:
            i = self._pending.pop(0)
            size = self._doc.page_size(i)
            tw, th = self._fit(size.width, size.height)
            scale = tw * dpr / size.width
            img = to_qimage(self._doc.render_page(i, scale))
            canvas = QPixmap(round(THUMB_W * dpr), round(THUMB_H * dpr))
            canvas.setDevicePixelRatio(dpr)
            canvas.fill(Qt.GlobalColor.transparent)
            p = QPainter(canvas)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            x, y = (THUMB_W - tw) / 2, (THUMB_H - th) / 2
            p.drawImage(QRectF(x, y, tw, th), img)
            p.setPen(QColor(0, 0, 0, 60))
            p.drawRect(QRectF(x, y, tw - 1, th - 1))
            p.end()
            item = self.item(i)
            if item is not None:
                item.setIcon(QIcon(canvas))

