"""Vista continua de páginas con zoom y renderizado perezoso.

La escena usa coordenadas en puntos PDF (sin escalar): cada página es un
`PageItem` de tamaño (ancho, alto) en puntos, apiladas verticalmente. El zoom es
la transformación de la vista. Así las herramientas de edición pueden traducir
un clic a coordenadas de página sin conocer el zoom.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QKeyEvent,
    QMouseEvent,
    QNativeGestureEvent,
    QPainter,
    QPalette,
    QPixmap,
    QResizeEvent,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsProxyWidget,
    QGraphicsScene,
    QGraphicsView,
    QStyleOptionGraphicsItem,
    QWidget,
)

from xpdf.core.document import Document
from xpdf.ui.qt_image import to_qpixmap

PAGE_GAP = 16.0  # puntos entre páginas
MIN_ZOOM = 0.1
MAX_ZOOM = 6.0
ZOOM_STEPS = (0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0)
#: Límite de píxeles por página renderizada (~40 Mpx ≈ 120 MB RGB) para zooms extremos.
MAX_RENDER_PIXELS = 40_000_000
#: Páginas fuera de pantalla que se conservan renderizadas a cada lado.
KEEP_MARGIN_PAGES = 2


class PageItem(QGraphicsItem):
    """Una página: fondo blanco + mapa de bits renderizado (si lo hay)."""

    def __init__(self, index: int, width: float, height: float) -> None:
        super().__init__()
        self.index = index
        self._rect = QRectF(0, 0, width, height)
        self.pixmap: QPixmap | None = None
        #: Si está, se muestra en lugar del render normal (p. ej. página sin el
        #: párrafo que se está editando).
        self.override: QPixmap | None = None
        self.render_scale = 0.0
        self.setCacheMode(QGraphicsItem.CacheMode.NoCache)

    def boundingRect(self) -> QRectF:
        return self._rect

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self._rect, Qt.GlobalColor.white)
        shown = self.override if self.override is not None else self.pixmap
        if shown is not None:
            painter.drawPixmap(self._rect, shown, QRectF(shown.rect()))
        painter.setPen(QColor(0, 0, 0, 60))
        painter.drawRect(self._rect)


class ViewTool:
    """Interfaz de las herramientas: reciben los eventos del ratón y teclado.

    Devuelven True si consumen el evento. Ver `xpdf.ui.tools`.
    """

    def activate(self) -> None: ...
    def deactivate(self) -> None: ...
    def mouse_press(self, page: int, pos: QPointF, event: QMouseEvent) -> bool:
        return False
    def mouse_move(self, page: int | None, pos: QPointF | None, event: QMouseEvent) -> bool:
        return False
    def mouse_release(self, page: int | None, pos: QPointF | None, event: QMouseEvent) -> bool:
        return False
    def key_press(self, event: QKeyEvent) -> bool:
        return False
    def context_menu(self, page: int, pos: QPointF, global_pos: QPoint) -> bool:
        return False
    def cursor(self) -> Qt.CursorShape:
        return Qt.CursorShape.ArrowCursor


class PageView(QGraphicsView):
    zoomChanged = Signal(float)
    currentPageChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._doc: Document | None = None
        self._pages: list[PageItem] = []
        self._zoom = 1.0
        self._fit_width = True
        self._current = -1
        self._tool: ViewTool | None = None
        self.setMouseTracking(True)

        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self._apply_background()

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(30)
        self._render_timer.timeout.connect(self._render_visible)
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self.horizontalScrollBar().valueChanged.connect(self._on_scrolled)

    # ------------------------------------------------------------------ público
    def set_document(self, doc: Document | None) -> None:
        self._doc = doc
        self._scene.clear()
        self._pages = []
        self._current = -1
        if doc is None:
            self._scene.setSceneRect(QRectF())
            return
        y = 0.0
        max_w = 0.0
        for i in range(doc.page_count):
            size = doc.page_size(i)
            item = PageItem(i, size.width, size.height)
            item.setPos(-size.width / 2, y)
            self._scene.addItem(item)
            self._pages.append(item)
            y += size.height + PAGE_GAP
            max_w = max(max_w, size.width)
        self._scene.setSceneRect(
            QRectF(-max_w / 2 - PAGE_GAP, -PAGE_GAP, max_w + 2 * PAGE_GAP, y + PAGE_GAP)
        )
        self._fit_width = True
        self._apply_fit_width()
        self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
        self._update_current_page()
        self._schedule_render()

    def reload(self) -> None:
        """Rehace la escena (tras cambios en páginas) conservando zoom y posición."""
        if self._doc is None:
            return
        page = max(self._current, 0)
        fit = self._fit_width
        zoom = self._zoom
        self.set_document(self._doc)
        if not fit:
            self.set_zoom(zoom)
        self.scroll_to_page(max(0, min(page, len(self._pages) - 1)))

    def sync(self) -> None:
        """Tras un cambio del documento: si cambió el número o el tamaño de las
        páginas (borrar, girar, reordenar...) se rehace la escena; si no, solo se
        vuelve a renderizar (y no se mueve el desplazamiento)."""
        if self._doc is None:
            return
        same = self._doc.page_count == len(self._pages) and all(
            abs(self._doc.page_size(i).width - it.boundingRect().width()) < 0.01
            and abs(self._doc.page_size(i).height - it.boundingRect().height()) < 0.01
            for i, it in enumerate(self._pages)
        )
        if same:
            self.invalidate_all()
        else:
            self.reload()

    def invalidate_page(self, index: int) -> None:
        """Fuerza a re-renderizar una página (tras editarla)."""
        if 0 <= index < len(self._pages):
            self._pages[index].render_scale = 0.0
            self._schedule_render()

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def current_page(self) -> int:
        return self._current

    def set_zoom(self, zoom: float, anchor: QPointF | None = None) -> None:
        """Cambia el zoom. `anchor` (en coordenadas del viewport) queda fijo."""
        self._fit_width = False
        self._set_zoom(zoom, anchor)

    def zoom_in(self) -> None:
        nxt = next((z for z in ZOOM_STEPS if z > self._zoom + 1e-3), MAX_ZOOM)
        self.set_zoom(nxt)

    def zoom_out(self) -> None:
        prev = next((z for z in reversed(ZOOM_STEPS) if z < self._zoom - 1e-3), MIN_ZOOM)
        self.set_zoom(prev)

    def refit(self) -> None:
        """Reaplica «ajustar al ancho» si está activo (p. ej. tras mostrarse la vista)."""
        if self._fit_width:
            self._apply_fit_width()

    def fit_width(self) -> None:
        self._fit_width = True
        self._apply_fit_width()

    def scroll_to_page(self, index: int) -> None:
        if not 0 <= index < len(self._pages):
            return
        item = self._pages[index]
        top = self.mapFromScene(item.scenePos() - QPointF(0, PAGE_GAP / 2))
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + top.y())

    # ------------------------------------------------------------ herramientas
    def set_tool(self, tool: ViewTool | None) -> None:
        if self._tool is not None:
            self._tool.deactivate()
        self._tool = tool
        if tool is not None:
            tool.activate()
            self.viewport().setCursor(tool.cursor())
        else:
            self.viewport().unsetCursor()

    def page_item(self, index: int) -> PageItem | None:
        return self._pages[index] if 0 <= index < len(self._pages) else None

    def set_page_override(self, index: int, pixmap: QPixmap | None) -> None:
        item = self.page_item(index)
        if item is not None:
            item.override = pixmap
            item.update()

    def invalidate_all(self) -> None:
        """Re-renderiza todo (tras deshacer/rehacer: el documento cambió)."""
        for item in self._pages:
            item.render_scale = 0.0
        self._schedule_render()

    def _over_widget(self, event: QMouseEvent) -> bool:
        """¿El ratón está sobre un widget incrustado (el editor de texto)?"""
        item = self.itemAt(event.position().toPoint())
        while item is not None:
            if isinstance(item, QGraphicsProxyWidget):
                return True
            item = item.parentItem()
        return False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._over_widget(event):
            super().mousePressEvent(event)
            return
        hit = self.page_at(self.mapToScene(event.position().toPoint()))
        if self._tool is not None and hit is not None and self._tool.mouse_press(hit[0], hit[1], event):
            event.accept()
            return
        if self._tool is not None and hit is None and self._tool.mouse_press(-1, QPointF(), event):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._over_widget(event):
            item = self.itemAt(event.position().toPoint())
            if item is not None and item.hasCursor():
                self.viewport().setCursor(item.cursor())  # p. ej. tiradores de ancho
            else:
                self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
            super().mouseMoveEvent(event)
            return
        if self._tool is not None:
            self.viewport().setCursor(self._tool.cursor())
        hit = self.page_at(self.mapToScene(event.position().toPoint()))
        if self._tool is not None and self._tool.mouse_move(
            hit[0] if hit else None, hit[1] if hit else None, event
        ):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._over_widget(event):
            super().mouseReleaseEvent(event)
            return
        hit = self.page_at(self.mapToScene(event.position().toPoint()))
        if self._tool is not None and self._tool.mouse_release(
            hit[0] if hit else None, hit[1] if hit else None, event
        ):
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        hit = self.page_at(self.mapToScene(event.pos()))
        if self._tool is not None and hit is not None and self._tool.context_menu(hit[0], hit[1], event.globalPos()):
            event.accept()
            return
        super().contextMenuEvent(event)

    def to_viewport(self, page: int, pos: QPointF) -> QPointF:
        """Punto de la página (puntos PDF) -> píxeles del área visible."""
        item = self.page_item(page)
        if item is None:
            return QPointF()
        return QPointF(self.mapFromScene(item.mapToScene(pos)))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._tool is not None and self._tool.key_press(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def page_at(self, scene_pos: QPointF) -> tuple[int, QPointF] | None:
        """Página bajo `scene_pos` y el punto en coordenadas de esa página (puntos)."""
        for item in self._pages:
            local = item.mapFromScene(scene_pos)
            if item.boundingRect().contains(local):
                return item.index, local
        return None

    # ------------------------------------------------------------------ zoom
    def _set_zoom(self, zoom: float, anchor: QPointF | None = None) -> None:
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        if abs(zoom - self._zoom) < 1e-4 and not self.transform().isIdentity():
            return
        if anchor is None:
            anchor = QPointF(self.viewport().rect().center())
        scene_anchor = self.mapToScene(anchor.toPoint())
        self._zoom = zoom
        self.setTransform(QTransform.fromScale(zoom, zoom))
        # recolocar para que el punto de escena bajo el ancla no se mueva
        new_vp = self.mapFromScene(scene_anchor)
        delta = new_vp - anchor.toPoint()
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + delta.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + delta.y())
        self.zoomChanged.emit(zoom)
        self._schedule_render()

    def _apply_fit_width(self) -> None:
        if not self._pages:
            return
        max_w = max(p.boundingRect().width() for p in self._pages)
        # El sceneRect incluye un margen PAGE_GAP a cada lado; -2 px por redondeo.
        avail = self.viewport().width() - 2
        if avail > 50:
            top_anchor = QPointF(self.viewport().width() / 2, 0)
            self._set_zoom(avail / (max_w + 2 * PAGE_GAP), top_anchor)

    # ------------------------------------------------------------------ eventos
    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            if steps:
                self.set_zoom(self._zoom * (1.15**steps), event.position())
            event.accept()
            return
        super().wheelEvent(event)

    def viewportEvent(self, event: object) -> bool:
        # Pellizco en el trackpad (macOS)
        if (
            isinstance(event, QNativeGestureEvent)
            and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture
        ):
            self.set_zoom(self._zoom * (1.0 + event.value()), event.position())
            return True
        return super().viewportEvent(event)  # type: ignore[arg-type]

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._fit_width:
            self._apply_fit_width()
        self._schedule_render()

    def changeEvent(self, event: object) -> None:
        super().changeEvent(event)  # type: ignore[arg-type]
        self._apply_background()

    def _apply_background(self) -> None:
        base = self.palette().color(QPalette.ColorRole.Window)
        bg = base.darker(115) if base.lightness() > 128 else base.darker(140)
        self.setBackgroundBrush(bg)

    def _on_scrolled(self) -> None:
        self._update_current_page()
        self._schedule_render()

    # ------------------------------------------------------------ renderizado
    def _schedule_render(self) -> None:
        self._render_timer.start()

    def _visible_scene_rect(self) -> QRectF:
        return self.mapToScene(self.viewport().rect()).boundingRect()

    def _render_visible(self) -> None:
        if self._doc is None or not self._pages:
            return
        visible = self._visible_scene_rect()
        dpr = self.devicePixelRatioF()
        vis_idx = [p.index for p in self._pages if p.sceneBoundingRect().intersects(visible)]
        if not vis_idx:
            return
        lo = max(0, vis_idx[0] - KEEP_MARGIN_PAGES)
        hi = min(len(self._pages) - 1, vis_idx[-1] + KEEP_MARGIN_PAGES)
        for item in self._pages:
            if item.index < lo or item.index > hi:
                if item.pixmap is not None:
                    item.pixmap = None
                    item.render_scale = 0.0
                continue
            if item.index not in vis_idx and item.pixmap is not None:
                continue  # fuera de pantalla: conservar lo que haya, sin re-renderizar
            rect = item.boundingRect()
            scale = self._zoom * dpr
            pixels = rect.width() * rect.height() * scale * scale
            if pixels > MAX_RENDER_PIXELS:
                scale *= (MAX_RENDER_PIXELS / pixels) ** 0.5
            if abs(item.render_scale - scale) < 1e-3:
                continue
            item.pixmap = to_qpixmap(self._doc.render_page(item.index, scale), dpr)
            item.render_scale = scale
            item.update()

    def _update_current_page(self) -> None:
        if not self._pages:
            return
        visible = self._visible_scene_rect()
        # La página actual es la que ocupa más superficie visible.
        best, best_area = self._current, -1.0
        for item in self._pages:
            inter = item.sceneBoundingRect().intersected(visible)
            area = inter.width() * inter.height()
            if area > best_area:
                best, best_area = item.index, area
        if best != self._current:
            self._current = best
            self.currentPageChanged.emit(best)
