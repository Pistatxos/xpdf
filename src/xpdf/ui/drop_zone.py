"""Zona para soltar un PDF: pantalla de bienvenida y aviso al arrastrar.

- `WelcomeView`: sin documento abierto, una zona grande «Arrastra aquí tu PDF»
  con botón «Abrir un PDF…». Se ilumina mientras se arrastra un PDF encima.
- `DropOverlay`: con un documento abierto, un aviso «Suelta para abrir…» que
  cubre la vista mientras se arrastra otro PDF.

Ninguna acepta el soltar por sí misma: lo resuelve la ventana principal, que ya
gestiona el arrastre (así no hay dos caminos para abrir).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from xpdf.ui.icons import icon

_ACCENT = QColor(0, 120, 215)


def _card_rect(w: QWidget, max_w: float = 560, h: float = 330) -> QRectF:
    width = min(max_w, w.width() - 80)
    return QRectF((w.width() - width) / 2, (w.height() - h) / 2, width, h)


def _paint_card(p: QPainter, rect: QRectF, pal: QPalette, active: bool) -> None:
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    base = pal.color(QPalette.ColorRole.Base)
    if active:
        fill = QColor(_ACCENT)
        fill.setAlpha(28)
        p.setBrush(base)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 18, 18)
        p.setBrush(fill)
        border = QPen(_ACCENT, 2.5, Qt.PenStyle.SolidLine)
    else:
        p.setBrush(base)
        border = QPen(pal.color(QPalette.ColorRole.Mid), 2, Qt.PenStyle.DashLine)
        border.setDashPattern([6, 5])
    p.setPen(border)
    p.drawRoundedRect(rect, 18, 18)


class WelcomeView(QWidget):
    openRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._active = False
        self.setAutoFillBackground(False)

        self._icon = QLabel(self)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title = QLabel("Arrastra aquí tu PDF", self)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont(self._title.font())
        f.setPointSizeF(f.pointSizeF() * 1.6)
        f.setWeight(QFont.Weight.DemiBold)
        self._title.setFont(f)
        self._hint = QLabel("o", self)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._button = QPushButton("  Abrir un PDF…", self)
        self._button.setIcon(icon("open"))
        self._button.setDefault(True)
        self._button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._button.setMinimumHeight(36)
        self._button.clicked.connect(self.openRequested)

        lay = QVBoxLayout(self)
        lay.addStretch(1)
        lay.addWidget(self._icon)
        lay.addSpacing(10)
        lay.addWidget(self._title)
        lay.addSpacing(4)
        lay.addWidget(self._hint)
        lay.addSpacing(8)
        lay.addWidget(self._button, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch(1)
        self._refresh()

    def set_drag_active(self, active: bool, name: str = "") -> None:
        self._active = active
        self._title.setText(f"Suelta para abrir «{name}»" if active and name else "Arrastra aquí tu PDF")
        self._hint.setVisible(not active)
        self._button.setVisible(not active)
        self._refresh()
        self.update()

    def _refresh(self) -> None:
        self._icon.setPixmap(icon("drop-file").pixmap(64, 64))
        muted = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
        self._hint.setStyleSheet(f"color: {muted.name()};")
        if self._active:
            self._title.setStyleSheet(f"color: {_ACCENT.name()};")
        else:
            self._title.setStyleSheet("")

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ThemeChange):
            self._refresh()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        pal = self.palette()
        window = pal.color(QPalette.ColorRole.Window)
        p.fillRect(self.rect(), window.darker(108) if window.lightness() > 128 else window.darker(125))
        _paint_card(p, _card_rect(self), pal, self._active)
        p.end()


class DropOverlay(QWidget):
    """Aviso translúcido «Suelta para abrir…» sobre el documento abierto."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._name = ""
        self.hide()

    def show_for(self, name: str) -> None:
        self._name = name
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self.raise_()
        self.show()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        shade = QColor(self.palette().color(QPalette.ColorRole.Window))
        shade.setAlpha(200)
        p.fillRect(self.rect(), shade)
        rect = _card_rect(self, 520, 150)
        _paint_card(p, rect, self.palette(), True)
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * 1.5)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(_ACCENT)
        text = f"Suelta para abrir «{self._name}»" if self._name else "Suelta para abrir el PDF"
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)
        p.end()
