"""Editor de texto superpuesto a la página (WYSIWYG).

Es un `QTextEdit` sin marco ni fondo dentro de la escena: como la escena está en
puntos PDF y la vista aplica el zoom, el editor escala con la página. Cada tramo
lleva en su formato el índice del estilo original, de modo que el texto nuevo
hereda el estilo de donde se escribe y al confirmar se devuelven `Span`s.
"""

from __future__ import annotations

import hashlib
import logging

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFocusEvent,
    QFont,
    QFontDatabase,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
    QTextOption,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsItem,
    QGraphicsProxyWidget,
    QGraphicsRectItem,
    QGraphicsSceneMouseEvent,
    QTextEdit,
    QWidget,
)

from xpdf.core.editing import FontClass, ParagraphInfo, Span, StyleInfo

log = logging.getLogger(__name__)

STYLE_PROP = QTextFormat.Property.UserProperty + 1

_FALLBACK_FAMILIES: dict[FontClass, list[str]] = {
    "sans": ["Helvetica", "Arial", "Liberation Sans", "DejaVu Sans"],
    "serif": ["Times New Roman", "Times", "Liberation Serif", "DejaVu Serif"],
    "mono": ["Courier New", "Courier", "Liberation Mono", "DejaVu Sans Mono"],
}
_loaded_fonts: dict[str, str | None] = {}

_ALIGN = {
    "left": Qt.AlignmentFlag.AlignLeft,
    "center": Qt.AlignmentFlag.AlignHCenter,
    "right": Qt.AlignmentFlag.AlignRight,
    "justify": Qt.AlignmentFlag.AlignJustify,
}


def _load(data: bytes | None) -> str | None:
    if not data:
        return None
    key = hashlib.sha1(data).hexdigest()
    if key not in _loaded_fonts:
        fid = QFontDatabase.addApplicationFontFromData(data)
        fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        _loaded_fonts[key] = fams[0] if fams else None
    return _loaded_fonts[key]


def fallback_family_for(style: StyleInfo) -> str:
    """Familia para los caracteres que el recorte incrustado no puede dibujar."""
    return _load(style.fallback_data) or _generic(style)


def family_for(style: StyleInfo) -> str:
    """Familia Qt para mostrar el estilo: la fuente real si Qt puede cargarla."""
    return _load(style.font_data) or _generic(style)


def _generic(style: StyleInfo) -> str:
    available = set(QFontDatabase.families())
    for fam in _FALLBACK_FAMILIES[style.font_class]:
        if fam in available:
            return fam
    return QFont().defaultFamily()


def points_to_qt(widget: QWidget, size: float) -> float:
    """Tamaño en puntos PDF -> pointSize de Qt para que ocupe `size` unidades de escena."""
    return size * 72.0 / max(widget.logicalDpiY(), 1)


class _Edit(QTextEdit):
    committed = Signal()
    cancelled = Signal()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            e.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.committed.emit()
            return
        if e.matches(QKeySequence.StandardKey.Bold):
            fmt = QTextCharFormat()
            bold = self.currentCharFormat().fontWeight() < QFont.Weight.Bold
            fmt.setFontWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
            self.mergeCurrentCharFormat(fmt)
            return
        if e.matches(QKeySequence.StandardKey.Italic):
            fmt = QTextCharFormat()
            fmt.setFontItalic(not self.currentCharFormat().fontItalic())
            self.mergeCurrentCharFormat(fmt)
            return
        super().keyPressEvent(e)

    def focusOutEvent(self, e: QFocusEvent) -> None:
        super().focusOutEvent(e)


_MIN_WIDTH = 24.0
_ACCENT = QColor(0, 120, 215)


class _Handle(QGraphicsRectItem):
    """Tirador lateral para cambiar el ancho de la caja de texto.

    Se dibuja a tamaño fijo en pantalla (ignora el zoom) y con una zona para
    agarrar más amplia que el dibujo, para que sea fácil de usar.
    """

    W, H = 10.0, 30.0  # píxeles de pantalla
    GRAB = 8.0  # margen extra alrededor para agarrarlo

    GAP = 4.0  # separación del borde de la caja, para no tapar el texto

    def __init__(self, overlay: TextEditorOverlay, side: str) -> None:
        # El dibujo queda fuera de la caja: a la izquierda o a la derecha del borde.
        x = -self.GAP - self.W if side == "left" else self.GAP
        super().__init__(QRectF(x, -self.H / 2, self.W, self.H), overlay)
        self.overlay = overlay
        self.side = side
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setBrush(QBrush(_ACCENT))
        self.setPen(QPen(QColor(255, 255, 255), 1.5))
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setAcceptHoverEvents(True)
        self.setZValue(2)
        self.setToolTip("Arrastra para cambiar el ancho del texto")
        self._start_x = 0.0
        self._start_geom = (0.0, 0.0)

    def boundingRect(self) -> QRectF:
        g = self.GRAB
        return self.rect().adjusted(-g, -g, g, g)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRect(self.boundingRect())  # zona de agarre amplia
        return path

    def paint(self, painter: QPainter, option: object, widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(self.brush())
        painter.setPen(self.pen())
        painter.drawRoundedRect(self.rect(), self.W / 2, self.W / 2)
        # dos rayitas de «agarre»
        painter.setPen(QPen(QColor(255, 255, 255, 200), 1))
        cx = self.rect().center().x()
        for dx in (-1.5, 1.5):
            painter.drawLine(QPointF(cx + dx, -6), QPointF(cx + dx, 6))

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self._start_x = event.scenePos().x()
        self._start_geom = (self.overlay.pos().x(), float(self.overlay.edit.width()))
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        # La escena está en puntos PDF; el padre (página) no está escalado.
        dx = event.scenePos().x() - self._start_x
        x0, w0 = self._start_geom
        if self.side == "right":
            self.overlay.set_box(x0, max(_MIN_WIDTH, w0 + dx))
        else:
            dx = min(dx, w0 - _MIN_WIDTH)
            self.overlay.set_box(x0 + dx, w0 - dx)
        event.accept()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.overlay.focus()
        event.accept()


class TextEditorOverlay(QGraphicsProxyWidget):
    """Editor colocado sobre la página `page_item` en coordenadas de página."""

    def __init__(self, parent_item: QGraphicsItem) -> None:
        super().__init__(parent_item)
        self.edit = _Edit()
        self.edit.setFrameShape(QFrame.Shape.NoFrame)
        self.edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.edit.setStyleSheet(
            "QTextEdit { background: transparent; border: none; selection-background-color:"
            " rgba(0, 120, 215, 90); selection-color: palette(text); }"
        )
        self.edit.viewport().setAutoFillBackground(False)
        self.edit.document().setDocumentMargin(0)
        self.edit.setAcceptRichText(False)
        self.edit.document().contentsChanged.connect(self._grow)
        self.edit.document().contentsChange.connect(self._cover_missing)
        self._fixing = False
        self.setWidget(self.edit)
        self.setZValue(10)
        self._styles: tuple[StyleInfo, ...] = ()
        self._min_height = 0.0
        self._wrap = True
        self._max_width = 10_000.0
        #: Alineación elegida por el usuario en la barra de formato (None = la original).
        self.align_override: str | None = None
        #: Caja (x0, x1) fijada por el usuario con los tiradores (None = la original).
        self.box_override: tuple[float, float] | None = None
        # Marco discontinuo y tiradores para cambiar el ancho.
        self._frame = QGraphicsRectItem(self)
        pen = QPen(_ACCENT, 0, Qt.PenStyle.DashLine)  # cosmético: 1 px a cualquier zoom
        self._frame.setPen(pen)
        self._frame.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._frame.setZValue(1)
        self._handles = (_Handle(self, "left"), _Handle(self, "right"))

    # ------------------------------------------------------------- configurar
    def load_paragraph(self, para: ParagraphInfo) -> None:
        """Muestra el párrafo con su aspecto original, listo para editar."""
        self._styles = para.styles
        x0, y0, x1, y1 = para.bbox
        doc = self.edit.document()
        doc.clear()
        cursor = QTextCursor(doc)
        block = QTextBlockFormat()
        block.setAlignment(_ALIGN[para.align])
        block.setLineHeight(para.line_pitch, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        block.setTextIndent(para.indent)
        cursor.setBlockFormat(block)
        for text, idx in para.spans:
            cursor.insertText(text, self._format(para.styles[idx]))
        self._wrap = True
        self.edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        bx0, bx1 = para.box
        self._place(QRectF(bx0, y0, (bx1 - bx0) + 1.5, max(y1 - y0, para.line_pitch)))
        # Se alinea la línea base del primer renglón de Qt con la del PDF.
        self.setPos(bx0, para.first_baseline - self._first_baseline_offset())
        self.edit.moveCursor(QTextCursor.MoveOperation.End)

    def _first_baseline_offset(self) -> float:
        layout = self.edit.document().begin().layout()
        if layout is None or layout.lineCount() == 0:
            return 0.0
        line = layout.lineAt(0)
        return float(line.y() + line.ascent())

    def start_new(self, pos: QPointF, style: StyleInfo, max_width: float) -> None:
        """Editor vacío para texto nuevo en `pos` (esquina superior izquierda)."""
        self._styles = (style,)
        doc = self.edit.document()
        doc.clear()
        cursor = QTextCursor(doc)
        block = QTextBlockFormat()
        block.setLineHeight(style.size * 1.2, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        cursor.setBlockFormat(block)
        self.edit.setCurrentCharFormat(self._format(style))
        self._wrap = False
        self.edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.edit.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self._max_width = max_width
        self._place(QRectF(pos.x(), pos.y(), 40.0, style.size * 1.4))

    def apply_style(self, style: StyleInfo) -> None:
        """Aplica un estilo a todo el texto (texto nuevo: un único estilo)."""
        self._styles = (style,)
        cursor = QTextCursor(self.edit.document())
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.setCharFormat(self._format(style))
        block = QTextBlockFormat()
        block.setLineHeight(style.size * 1.2, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        cursor.mergeBlockFormat(block)
        self.edit.setCurrentCharFormat(self._format(style))
        self._grow()

    def merge_char_format(self, fmt: QTextCharFormat) -> None:
        """Aplica formato a la selección (o a lo que se escriba a continuación)."""
        self.edit.mergeCurrentCharFormat(fmt)

    def set_alignment(self, align: str) -> None:
        self.align_override = align
        cursor = QTextCursor(self.edit.document())
        cursor.select(QTextCursor.SelectionType.Document)
        block = QTextBlockFormat()
        block.setAlignment(_ALIGN[align])
        cursor.mergeBlockFormat(block)

    def focus(self) -> None:
        self.setFocus()
        self.edit.setFocus()

    # --------------------------------------------------------------- resultado
    def plain_text(self) -> str:
        return self.edit.toPlainText()

    def spans(self) -> list[Span]:
        """Tramos con su estilo original y los cambios de formato aplicados."""
        out: list[Span] = []
        doc = self.edit.document()
        block = doc.begin()
        first = True
        while block.isValid():
            if not first and out:
                out.append(Span("\n", out[-1].style))
            first = False
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    out.append(self._span(frag.text(), frag.charFormat()))
                it += 1
            block = block.next()
        return out

    def _span(self, text: str, fmt: QTextCharFormat) -> Span:
        idx = fmt.property(STYLE_PROP)
        idx = int(idx) if isinstance(idx, int) else 0
        st = self._styles[idx] if 0 <= idx < len(self._styles) else self._styles[0]
        bold = fmt.fontWeight() >= QFont.Weight.Bold
        italic = fmt.fontItalic()
        size = self._size_of(fmt)
        color = fmt.foreground().color().rgb() & 0xFFFFFF
        return Span(
            text.replace(" ", "\n").replace(" ", "\n"),
            idx,
            bold=None if bold == st.bold else bold,
            italic=None if italic == st.italic else italic,
            size=None if size is None or abs(size - st.size) < 0.05 else size,
            color=None if color == st.color else color,
        )

    # ------------------------------------------------------------------ interno
    def _cover_missing(self, pos: int, removed: int, added: int) -> None:
        """Muestra con la fuente de respaldo los caracteres que el recorte no tiene.

        Las fuentes incrustadas recortadas declaran glifos que están vacíos: Qt no
        haría sustitución y el carácter saldría en blanco mientras se escribe.
        """
        if self._fixing or added <= 0:
            return
        self._fixing = True
        try:
            doc = self.edit.document()
            cur = QTextCursor(doc)
            for i in range(pos, min(pos + added, doc.characterCount() - 1)):
                cur.setPosition(i)
                cur.setPosition(i + 1, QTextCursor.MoveMode.KeepAnchor)
                ch = cur.selectedText()
                fmt = cur.charFormat()
                idx = fmt.property(STYLE_PROP)
                if not isinstance(idx, int) or not (0 <= idx < len(self._styles)):
                    continue
                st = self._styles[idx]
                if st.covered is None or not ch.strip():
                    continue
                # Lo que el recorte sí dibuja vuelve a su fuente (Qt hereda el
                # formato del carácter anterior, que pudo ser de respaldo).
                want = family_for(st) if ch in st.covered else fallback_family_for(st)
                if list(fmt.fontFamilies() or []) != [want]:
                    fix = QTextCharFormat()
                    fix.setFontFamilies([want])
                    cur.mergeCharFormat(fix)
        finally:
            self._fixing = False

    def _format(self, st: StyleInfo) -> QTextCharFormat:
        fmt = QTextCharFormat()
        f = QFont(family_for(st))
        f.setPointSizeF(points_to_qt(self.edit, st.size))
        f.setBold(st.bold)
        f.setItalic(st.italic)
        f.setKerning(True)
        fmt.setFont(f)
        fmt.setForeground(QColor((st.color >> 16) & 255, (st.color >> 8) & 255, st.color & 255))
        fmt.setProperty(STYLE_PROP, st.index)
        fmt.setProperty(STYLE_PROP + 1, st.size)  # tamaño en puntos PDF
        return fmt

    def _size_of(self, fmt: QTextCharFormat) -> float | None:
        pdf = fmt.property(STYLE_PROP + 1)
        qt = fmt.fontPointSize()
        if qt <= 0:
            return float(pdf) if isinstance(pdf, float) else None
        return qt * max(self.edit.logicalDpiY(), 1) / 72.0

    def set_box(self, x: float, width: float) -> None:
        """El usuario cambió el ancho con un tirador: el texto se recoloca."""
        self.setPos(x, self.pos().y())
        if not self._wrap:  # texto nuevo: a partir de ahora ajusta a este ancho
            self._wrap = True
            self.edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
            self.edit.setWordWrapMode(QTextOption.WrapMode.WordWrap)
        self.edit.setFixedWidth(max(1, round(width)))
        self.box_override = (x, x + width - 1.5)
        self._grow()

    def _place(self, rect: QRectF) -> None:
        self.setPos(rect.topLeft())
        self._min_height = rect.height()
        self.edit.setFixedWidth(max(1, round(rect.width())))
        self._grow()

    def _grow(self) -> None:
        """Ajusta el editor a su contenido: crece hacia abajo (y a lo ancho si no ajusta)."""
        doc = self.edit.document()
        if not self._wrap:
            doc.setTextWidth(-1)
            want = min(self._max_width, max(40.0, doc.idealWidth() + 6))
            self.edit.setFixedWidth(round(want))
        doc.setTextWidth(self.edit.viewport().width())
        h = max(self._min_height, doc.size().height() + 2)
        self.edit.setFixedHeight(round(h))
        self.resize(self.edit.width(), h)
        w = float(self.edit.width())
        self._frame.setRect(QRectF(-1, -1, w + 2, h + 2))
        # Centrados en los bordes; su tamaño en pantalla es fijo (ignoran el zoom).
        self._handles[0].setPos(-1, h / 2)
        self._handles[1].setPos(w + 1, h / 2)
