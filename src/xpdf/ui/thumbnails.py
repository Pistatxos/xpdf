"""Panel lateral de miniaturas."""

from __future__ import annotations

import time

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem, QWidget

from xpdf.core.document import Document
from xpdf.ui.qt_image import to_qimage

THUMB_W = 120
THUMB_H = 160
#: Tiempo máximo por tanda de renderizado para no bloquear la interfaz.
BATCH_BUDGET_S = 0.025


class ThumbnailPanel(QListWidget):
    pageActivated = Signal(int)

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

