"""Barra «Imagen»: opacidad, orden de capas, reemplazar y borrar.

Visible con una imagen seleccionada (herramienta Seleccionar).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel, QSlider, QToolBar, QWidget

from xpdf.core.images import ImageInfo
from xpdf.ui.icons import icon
from xpdf.ui.tools.image_tools import ImageSelectTool


class ImageBar(QToolBar):
    def __init__(self, tool: ImageSelectTool, parent: QWidget | None = None) -> None:
        super().__init__("Imagen", parent)
        self.tool = tool
        self.setMovable(False)
        self.setFloatable(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)

        self.addWidget(QLabel("  Opacidad ", self))
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(10, 100)
        self.slider.setSingleStep(5)
        self.slider.setPageStep(10)
        self.slider.setFixedWidth(160)
        self.slider.setToolTip("Opacidad de la imagen")
        self.addWidget(self.slider)
        self.value = QLabel("100 %", self)
        self.value.setMinimumWidth(48)
        self.addWidget(self.value)
        self.addSeparator()

        self.front = QAction("Traer al frente", self)
        self.behind = QAction("Detrás del texto", self)
        self.replace = QAction(icon("image"), "Reemplazar…", self)
        self.delete = QAction(icon("trash"), "Borrar", self)
        self.delete.setToolTip("Borrar la imagen (Supr)")
        for a in (self.front, self.behind):
            self.addAction(a)
        self.addSeparator()
        self.addAction(self.replace)
        self.addAction(self.delete)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        self.slider.valueChanged.connect(lambda v: self.value.setText(f"{v} %"))
        # Se aplica al soltar (un solo paso de deshacer), no en cada movimiento.
        self.slider.sliderReleased.connect(self._apply_opacity)
        self.slider.actionTriggered.connect(self._keyboard_step)
        self.front.triggered.connect(tool.to_front)
        self.behind.triggered.connect(tool.behind_text)
        self.replace.triggered.connect(tool.replace)
        self.delete.triggered.connect(tool.delete)

    def show_info(self, info: ImageInfo | None) -> None:
        self.setVisible(info is not None)
        if info is not None:
            self.slider.blockSignals(True)
            self.slider.setValue(round(info.opacity * 100))
            self.slider.blockSignals(False)
            self.value.setText(f"{round(info.opacity * 100)} %")

    def _apply_opacity(self) -> None:
        self.tool.set_opacity(self.slider.value() / 100)

    def _keyboard_step(self, action: int) -> None:
        # Flechas/AvPág sobre el deslizador (no hay «soltar»): aplicar al momento.
        if not self.slider.isSliderDown():
            self.slider.setValue(self.slider.sliderPosition())
            self._apply_opacity()
