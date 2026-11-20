"""Barra de formato de texto: fuente, tamaño, color, negrita, cursiva y alineación.

Visible con las herramientas de texto. En «Editar texto» aplica a la selección
del editor; en «Añadir texto» define el estilo del texto nuevo.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPixmap, QTextCharFormat
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QToolBar,
    QToolButton,
    QWidget,
)

from xpdf.core.editing import FontClass, NewTextStyle
from xpdf.ui.text_editor import STYLE_PROP, TextEditorOverlay, points_to_qt

_CLASSES: list[tuple[FontClass, str]] = [("sans", "Sans"), ("serif", "Serif"), ("mono", "Mono")]
_ALIGNS = [("left", "Izquierda"), ("center", "Centro"), ("right", "Derecha"), ("justify", "Justificado")]


class FormatBar(QToolBar):
    #: El estilo del texto nuevo cambió (modo «añadir»).
    newStyleChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Formato", parent)
        self.setMovable(False)
        self.setFloatable(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self._overlay: TextEditorOverlay | None = None
        self._mode = "edit"
        self._style = NewTextStyle()
        self._syncing = False

        self.font_box = QComboBox(self)
        for key, label in _CLASSES:
            self.font_box.addItem(label, key)
        self.font_box.setToolTip("Tipo de letra del texto nuevo")
        self.font_label = self.addWidget(self.font_box)

        self.size_box = QDoubleSpinBox(self)
        self.size_box.setRange(4, 144)
        self.size_box.setDecimals(1)
        self.size_box.setSingleStep(1)
        self.size_box.setSuffix(" pt")
        self.size_box.setValue(self._style.size)
        self.size_box.setToolTip("Tamaño de letra")
        self.addWidget(self.size_box)

        self.color_btn = QToolButton(self)
        self.color_btn.setToolTip("Color del texto")
        self._set_color_icon(QColor(0, 0, 0))
        self.addWidget(self.color_btn)

        self.bold_act = QAction("N", self)
        self.bold_act.setCheckable(True)
        self.bold_act.setToolTip("Negrita (Ctrl+B)")
        f = QFont()
        f.setBold(True)
        self.bold_act.setFont(f)
        self.addAction(self.bold_act)

        self.italic_act = QAction("K", self)
        self.italic_act.setCheckable(True)
        self.italic_act.setToolTip("Cursiva (Ctrl+I)")
        f = QFont()
        f.setItalic(True)
        self.italic_act.setFont(f)
        self.addAction(self.italic_act)

        self.addSeparator()
        self.align_box = QComboBox(self)
        for akey, alabel in _ALIGNS:
            self.align_box.addItem(alabel, akey)
        self.align_box.setToolTip("Alineación del párrafo")
        self.align_label = self.addWidget(self.align_box)

        self.hint = QLabel("", self)
        self.hint.setContentsMargins(12, 0, 0, 0)
        self.addWidget(self.hint)

        self.font_box.currentIndexChanged.connect(self._on_font)
        self.size_box.valueChanged.connect(self._on_size)
        self.color_btn.clicked.connect(self._on_color)
        self.bold_act.toggled.connect(self._on_bold)
        self.italic_act.toggled.connect(self._on_italic)
        self.align_box.currentIndexChanged.connect(self._on_align)
        self.set_mode("edit")

    # ------------------------------------------------------------------ estado
    def new_text_style(self) -> NewTextStyle:
        return self._style

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.font_label.setVisible(mode == "add")
        self.align_label.setVisible(mode == "edit")
        self._refresh_enabled()

    def attach(self, overlay: TextEditorOverlay | None) -> None:
        if self._overlay is not None:
            try:
                self._overlay.edit.currentCharFormatChanged.disconnect(self._sync_from)
            except (RuntimeError, TypeError):
                pass
        self._overlay = overlay
        if overlay is not None:
            overlay.edit.currentCharFormatChanged.connect(self._sync_from)
            self._sync_from(overlay.edit.currentCharFormat())
        self._refresh_enabled()

    def _refresh_enabled(self) -> None:
        active = self._mode == "add" or self._overlay is not None
        for w in (self.size_box, self.color_btn, self.align_box):
            w.setEnabled(active)
        self.bold_act.setEnabled(active)
        self.italic_act.setEnabled(active)
        if self._mode == "edit" and self._overlay is None:
            self.hint.setText("Haz clic en un párrafo para editarlo")
        elif self._mode == "add" and self._overlay is None:
            self.hint.setText("Haz clic en la página para escribir")
        else:
            self.hint.setText("")

    def _sync_from(self, fmt: QTextCharFormat) -> None:
        """Refleja en la barra el formato del cursor del editor."""
        if self._mode != "edit" or self._overlay is None:
            return
        self._syncing = True
        try:
            size = fmt.property(STYLE_PROP + 1)
            qt = fmt.fontPointSize()
            if qt > 0:
                size = qt * max(self._overlay.edit.logicalDpiY(), 1) / 72.0
            if isinstance(size, float):
                self.size_box.setValue(round(size, 1))
            self.bold_act.setChecked(fmt.fontWeight() >= QFont.Weight.Bold)
            self.italic_act.setChecked(fmt.fontItalic())
            self._set_color_icon(fmt.foreground().color())
        finally:
            self._syncing = False

    # ---------------------------------------------------------------- acciones
    def _merge(self, fmt: QTextCharFormat) -> None:
        if self._overlay is not None:
            self._overlay.merge_char_format(fmt)
            self._overlay.focus()

    def _update_new(self, **changes: object) -> None:
        from dataclasses import replace

        self._style = replace(self._style, **changes)  # type: ignore[arg-type]
        self.newStyleChanged.emit()

    def _on_font(self) -> None:
        if not self._syncing and self._mode == "add":
            self._update_new(font_class=self.font_box.currentData())

    def _on_size(self, value: float) -> None:
        if self._syncing:
            return
        if self._mode == "add":
            self._update_new(size=value)
        elif self._overlay is not None:
            fmt = QTextCharFormat()
            fmt.setFontPointSize(points_to_qt(self._overlay.edit, value))
            self._merge(fmt)

    def _on_color(self) -> None:
        current = QColor(
            (self._style.color >> 16) & 255, (self._style.color >> 8) & 255, self._style.color & 255
        )
        if self._overlay is not None and self._mode == "edit":
            current = self._overlay.edit.currentCharFormat().foreground().color()
        color = QColorDialog.getColor(current, self, "Color del texto")
        if not color.isValid():
            return
        self._set_color_icon(color)
        if self._mode == "add":
            self._update_new(color=color.rgb() & 0xFFFFFF)
        else:
            fmt = QTextCharFormat()
            fmt.setForeground(color)
            self._merge(fmt)

    def _on_bold(self, on: bool) -> None:
        if self._syncing:
            return
        if self._mode == "add":
            self._update_new(bold=on)
        else:
            fmt = QTextCharFormat()
            fmt.setFontWeight(QFont.Weight.Bold if on else QFont.Weight.Normal)
            self._merge(fmt)

    def _on_italic(self, on: bool) -> None:
        if self._syncing:
            return
        if self._mode == "add":
            self._update_new(italic=on)
        else:
            fmt = QTextCharFormat()
            fmt.setFontItalic(on)
            self._merge(fmt)

    def _on_align(self) -> None:
        if not self._syncing and self._overlay is not None and self._mode == "edit":
            self._overlay.set_alignment(str(self.align_box.currentData()))
            self._overlay.focus()

    def show_alignment(self, align: str) -> None:
        self._syncing = True
        try:
            idx = self.align_box.findData(align)
            if idx >= 0:
                self.align_box.setCurrentIndex(idx)
        finally:
            self._syncing = False

    def _set_color_icon(self, color: QColor) -> None:
        pm = QPixmap(16, 16)
        pm.fill(color)
        self.color_btn.setIcon(QIcon(pm))
