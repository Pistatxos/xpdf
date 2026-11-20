"""Ventana principal: barra de herramientas, miniaturas y vista de página."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QWidget,
)

from xpdf.core.commands import SnapshotCommand
from xpdf.core.document import Document
from xpdf.core.editing import NewTextStyle, TextEditing, TextOutcome
from xpdf.core.errors import XpdfError
from xpdf.core.history import History
from xpdf.core.pages import delete_command, move_command, rotate_command
from xpdf.ui.drop_zone import DropOverlay, WelcomeView
from xpdf.ui.format_bar import FormatBar
from xpdf.ui.icons import icon
from xpdf.ui.optimize_dialog import run_optimize_dialog
from xpdf.ui.page_view import PageView, ViewTool
from xpdf.ui.text_editor import TextEditorOverlay
from xpdf.ui.thumbnails import ThumbnailPanel
from xpdf.ui.tools.text_tools import AddTextTool, EditTextTool
from xpdf.ui.update_ui import UpdateController, show_about

log = logging.getLogger(__name__)

PDF_FILTER = "Documentos PDF (*.pdf)"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.doc: Document | None = None
        self.editing: TextEditing | None = None
        self.history = History()
        self.history.subscribe(self._on_history_changed)
        #: Si el usuario ya aceptó sobrescribir el original en esta sesión.
        self._overwrite_ok = False
        self._settings = QSettings()

        self.view = PageView(self)
        self.thumbs = ThumbnailPanel(self)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self.thumbs)
        splitter.addWidget(self.view)
        splitter.setStretchFactor(1, 1)
        splitter.setCollapsible(1, False)
        splitter.setSizes([170, 900])
        # Sin documento: zona de bienvenida «Arrastra aquí tu PDF»; con documento,
        # miniaturas + página (y un aviso encima al arrastrar otro PDF).
        self.welcome = WelcomeView(self)
        self.welcome.openRequested.connect(self.open_dialog)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.welcome)
        self.stack.addWidget(splitter)
        self.setCentralWidget(self.stack)
        # Hijo del contenedor y no del splitter: QSplitter convierte en panel
        # cualquier widget hijo suyo y el aviso le robaría espacio a la vista.
        self.drop_overlay = DropOverlay(self.stack)

        self._build_actions()
        self._build_toolbar()
        self._build_menus()
        self._build_statusbar()

        self.format_bar = FormatBar(self)
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.format_bar)
        self.format_bar.hide()
        self.edit_tool = EditTextTool(self)
        self.add_tool = AddTextTool(self)
        self.format_bar.newStyleChanged.connect(self.add_tool.restyle)
        self.tool_group.triggered.connect(self._on_tool_changed)

        self.updates = UpdateController(self, self._maybe_save)
        m = self.menuBar().addMenu("A&yuda")
        act = QAction("Buscar actualizaciones…", self)
        act.triggered.connect(lambda: self.updates.check(interactive=True))
        about = QAction("Acerca de xpdf", self)
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(lambda: show_about(self))
        m.addActions([act, about])
        self.updates.schedule_startup_check()

        self.view.zoomChanged.connect(self._on_zoom_changed)
        self.view.currentPageChanged.connect(self._on_page_changed)
        self.thumbs.pageActivated.connect(self.view.scroll_to_page)
        self.thumbs.pagesMoved.connect(self.move_pages)
        self.thumbs.deleteRequested.connect(self.delete_pages)
        self.thumbs.rotateRequested.connect(self.rotate_pages)

        self.setAcceptDrops(True)
        self.resize(1200, 860)
        if (geom := self._settings.value("window/geometry")) is not None:
            self.restoreGeometry(geom)
        self._update_state()

    # --------------------------------------------------------------- acciones
    def _act(
        self,
        text: str,
        slot: object,
        icon_name: str | None = None,
        shortcuts: list[QKeySequence] | None = None,
        tip: str | None = None,
    ) -> QAction:
        a = QAction(text, self)
        if icon_name:
            a.setIcon(icon(icon_name))
            a.setData(icon_name)
        if shortcuts:
            a.setShortcuts(shortcuts)
        hint = shortcuts[0].toString(QKeySequence.SequenceFormat.NativeText) if shortcuts else ""
        a.setToolTip(f"{tip or text} ({hint})" if hint else (tip or text))
        a.triggered.connect(slot)
        self.addAction(a)
        return a

    def _build_actions(self) -> None:
        K = QKeySequence
        S = QKeySequence.StandardKey
        self.act_open = self._act("Abrir…", self.open_dialog, "open", [K(S.Open)])
        self.act_save = self._act("Guardar", self.save, "save", [K(S.Save)])
        # SaveAs no tiene atajo estándar en Windows: se fija explícitamente.
        self.act_save_as = self._act("Guardar como…", self.save_as, None, [K("Ctrl+Shift+S")])
        self.act_close = self._act("Cerrar", self.close, None, [K(S.Close)])
        self.act_undo = self._act("Deshacer", self.undo, "undo", [K(S.Undo)])
        redo_keys = [K("Ctrl+Shift+Z")]
        if K(S.Redo) != redo_keys[0]:
            redo_keys.append(K(S.Redo))  # Ctrl+Y en Windows
        self.act_redo = self._act("Rehacer", self.redo, "redo", redo_keys)
        self.act_rot_left = self._act(
            "Girar a la izquierda", lambda: self.rotate_pages(-90), "rotate-left",
            [K("Ctrl+L")], "Girar las páginas seleccionadas a la izquierda",
        )
        self.act_rot_right = self._act(
            "Girar a la derecha", lambda: self.rotate_pages(90), "rotate-right",
            [K("Ctrl+R")], "Girar las páginas seleccionadas a la derecha",
        )
        self.act_delete_pages = self._act(
            "Borrar páginas", lambda: self.delete_pages(), "trash", None,
            "Borrar las páginas seleccionadas (Supr en las miniaturas)",
        )
        self.act_optimize = self._act(
            "Reducir tamaño…", self.optimize, None, None, "Guardar una copia que ocupe menos"
        )
        self.act_zoom_in = self._act("Acercar", self.view.zoom_in, "zoom-in", [K(S.ZoomIn), K("Ctrl+=")])
        self.act_zoom_out = self._act("Alejar", self.view.zoom_out, "zoom-out", [K(S.ZoomOut)])
        self.act_fit = self._act("Ajustar al ancho", self.view.fit_width, "fit-width", [K("Ctrl+0")])
        self.act_actual = self._act("Tamaño real", lambda: self.view.set_zoom(1.0), None, [K("Ctrl+1")])
        self.act_sidebar = self._act(
            "Miniaturas", self._toggle_sidebar, "sidebar", [K("Ctrl+Alt+1")], "Mostrar/ocultar miniaturas"
        )
        self.act_sidebar.setCheckable(True)
        self.act_sidebar.setChecked(True)

        # Herramientas en modo exclusivo. Solo «Seleccionar» está activa en la fase 1.
        self.tool_group = QActionGroup(self)
        self.tool_group.setExclusive(True)
        self.tools: dict[str, QAction] = {}
        for key, text, ic, sc in (
            ("select", "Seleccionar", "select", "V"),
            ("edit_text", "Editar texto", "edit-text", "E"),
            ("add_text", "Añadir texto", "add-text", "T"),
            ("image", "Imagen", "image", "I"),
        ):
            a = self._act(text, lambda: None, ic, [QKeySequence(sc)])
            a.setCheckable(True)
            self.tool_group.addAction(a)
            self.tools[key] = a
        self.tools["select"].setChecked(True)
        self.tools["image"].setEnabled(False)  # fase 4
        self.tools["image"].setToolTip("Imagen (próximamente)")

    def _build_toolbar(self) -> None:
        tb = QToolBar("Herramientas", self)
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        tb.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        tb.addAction(self.act_sidebar)
        tb.addSeparator()
        tb.addAction(self.act_open)
        tb.addAction(self.act_save)
        tb.addSeparator()
        tb.addAction(self.act_undo)
        tb.addAction(self.act_redo)
        tb.addSeparator()
        for a in self.tools.values():
            tb.addAction(a)
        tb.addSeparator()
        tb.addActions([self.act_rot_left, self.act_rot_right, self.act_delete_pages])
        spacer = QWidget(tb)
        spacer.setSizePolicy(spacer.sizePolicy().Policy.Expanding, spacer.sizePolicy().Policy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.act_zoom_out)
        self.zoom_label = QLabel("100 %", tb)
        self.zoom_label.setMinimumWidth(52)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tb.addWidget(self.zoom_label)
        tb.addAction(self.act_zoom_in)
        tb.addAction(self.act_fit)
        self.toolbar = tb

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&Archivo")
        m.addActions([self.act_open, self.act_save, self.act_save_as])
        m.addAction(self.act_optimize)
        m.addSeparator()
        m.addAction(self.act_close)
        m = mb.addMenu("&Edición")
        m.addActions([self.act_undo, self.act_redo])
        m.addSeparator()
        # Las herramientas viven en Edición: es donde se buscan «editar» y «añadir texto».
        m.addActions(list(self.tools.values()))
        m = mb.addMenu("&Ver")
        m.addActions([self.act_zoom_in, self.act_zoom_out, self.act_fit, self.act_actual])
        m.addSeparator()
        m.addAction(self.act_sidebar)
        m = mb.addMenu("&Páginas")
        m.addActions([self.act_rot_left, self.act_rot_right])
        m.addSeparator()
        m.addAction(self.act_delete_pages)

    def _build_statusbar(self) -> None:
        self.page_label = QLabel("", self)
        self.statusBar().addPermanentWidget(self.page_label)
        self.statusBar().setSizeGripEnabled(False)

    # ----------------------------------------------------------------- abrir
    def open_dialog(self) -> None:
        if not self._maybe_save():
            return
        start = str(self._settings.value("paths/last_dir", str(Path.home())))
        path, _ = QFileDialog.getOpenFileName(self, "Abrir PDF", start, PDF_FILTER)
        if path:
            self.open_path(path)

    def open_path(self, path: str | Path) -> bool:
        try:
            doc = Document.open(path)
        except XpdfError as err:
            self._error("No se pudo abrir el archivo", str(err))
            return False
        self._finish_editing(commit=False)
        if self.doc is not None:
            self.doc.close()
        self.doc = doc
        self.editing = TextEditing(doc)
        self._overwrite_ok = False
        self.history.clear()
        self._settings.setValue("paths/last_dir", str(Path(path).parent))
        # Primero se muestra la vista del documento: si sigue oculta tras la
        # bienvenida no tiene anchura y «ajustar al ancho» calcularía un zoom mínimo.
        self.stack.setCurrentIndex(1)
        self.view.set_document(doc)
        self.thumbs.set_document(doc)
        # Cuando el diseño ya ha dado su tamaño real a la vista, se reajusta.
        QTimer.singleShot(0, self.view.refit)
        self._update_state()
        return True

    # ---------------------------------------------------------------- guardar
    def save(self) -> bool:
        if self.doc is None:
            return False
        self._finish_editing(commit=True)
        if self.doc.path is None:
            return self.save_as()
        if not self._overwrite_ok:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Guardar")
            box.setText(f"¿Sobrescribir el archivo original «{self.doc.path.name}»?")
            box.setInformativeText("Puedes guardar los cambios en un archivo nuevo para conservar el original.")
            overwrite = box.addButton("Sobrescribir", QMessageBox.ButtonRole.AcceptRole)
            save_as = box.addButton("Guardar como…", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(save_as)
            box.exec()
            clicked = box.clickedButton()
            if clicked is save_as:
                return self.save_as()
            if clicked is not overwrite:
                return False
            self._overwrite_ok = True
        return self._do_save(None)

    def save_as(self) -> bool:
        if self.doc is None:
            return False
        self._finish_editing(commit=True)
        suggested = (
            str(self.doc.path.with_name(self.doc.path.stem + " (editado).pdf"))
            if self.doc.path
            else str(Path.home() / "documento.pdf")
        )
        path, _ = QFileDialog.getSaveFileName(self, "Guardar como", suggested, PDF_FILTER)
        if not path:
            return False
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        ok = self._do_save(Path(path))
        if ok:
            # El diálogo del sistema ya confirma si el destino existe.
            self._overwrite_ok = True
        return ok

    def _do_save(self, path: Path | None) -> bool:
        assert self.doc is not None
        try:
            if path is None:
                self.doc.save()
            else:
                self.doc.save_as(path)
        except XpdfError as err:
            self._error("No se pudo guardar", str(err))
            return False
        self.history.mark_clean()
        self.statusBar().showMessage("Guardado", 3000)
        self._update_state()
        return True

    def _maybe_save(self) -> bool:
        """Devuelve False si el usuario cancela."""
        self._finish_editing(commit=True)
        if self.doc is None or self.history.is_clean:
            return True
        name = self.doc.path.name if self.doc.path else "el documento"
        res = QMessageBox.question(
            self,
            "Cambios sin guardar",
            f"¿Guardar los cambios en «{name}» antes de continuar?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if res == QMessageBox.StandardButton.Save:
            return self.save()
        return res == QMessageBox.StandardButton.Discard

    # ---------------------------------------------------------- herramientas
    def _on_tool_changed(self, action: QAction) -> None:
        tool: ViewTool | None = None
        mode = ""
        if action is self.tools["edit_text"]:
            tool, mode = self.edit_tool, "edit"
        elif action is self.tools["add_text"]:
            tool, mode = self.add_tool, "add"
        self.view.set_tool(tool)
        self.format_bar.setVisible(bool(mode))
        if mode:
            self.format_bar.set_mode(mode)
            self.format_bar.attach(None)

    def _finish_editing(self, commit: bool) -> None:
        """Cierra el editor abierto (confirmando o descartando)."""
        for tool in (self.edit_tool, self.add_tool):
            if tool.overlay is not None:
                tool.commit() if commit else tool.cancel()
                if tool.overlay is not None:  # no se pudo confirmar
                    tool.cancel()

    # API para las herramientas (TextToolHost)
    def run_command(self, cmd: SnapshotCommand[TextOutcome]) -> TextOutcome | None:
        try:
            self.history.push(cmd)
        except XpdfError as err:
            self._error("No se pudo aplicar el cambio", str(err))
            return None
        except Exception:
            log.exception("Error aplicando %s", cmd.label)
            self._error(
                "No se pudo aplicar el cambio",
                "Ha ocurrido un error inesperado; el documento no se ha modificado.",
            )
            return None
        return cmd.result

    def show_status(self, message: str, timeout_ms: int = 4000) -> None:
        if message:
            self.statusBar().showMessage(message, timeout_ms)
        else:
            self.statusBar().clearMessage()

    def new_text_style(self) -> NewTextStyle:
        return self.format_bar.new_text_style()

    def editor_changed(self, overlay: TextEditorOverlay | None, mode: str) -> None:
        self.format_bar.attach(overlay)
        # Mientras se escribe, sus atajos (Ctrl+L/R, Supr) no deben tocar las páginas.
        for a in (self.act_rot_left, self.act_rot_right, self.act_delete_pages):
            a.setEnabled(overlay is None and self.doc is not None)
        if overlay is not None and mode == "edit" and self.edit_tool.para is not None:
            self.format_bar.show_alignment(self.edit_tool.para.align)

    # ------------------------------------------------------------- historial
    def undo(self) -> None:
        self._finish_editing(commit=False)
        self.history.undo()

    def redo(self) -> None:
        self._finish_editing(commit=False)
        self.history.redo()

    def _on_history_changed(self) -> None:
        # Cualquier paso del historial puede haber cambiado el documento (texto
        # o estructura): la vista decide si basta re-renderizar o hay que rehacerla.
        if self.doc is not None:
            self.view.sync()
            self.thumbs.refresh()
        self._update_state()

    # ---------------------------------------------------------------- páginas
    def _target_pages(self, pages: list[int] | None) -> list[int]:
        if pages:
            return pages
        sel = self.thumbs.selected_pages()
        return sel or ([self.view.current_page] if self.view.current_page >= 0 else [])

    def _run_page_command(self, cmd: SnapshotCommand[object]) -> bool:
        self._finish_editing(commit=True)
        try:
            self.history.push(cmd)
        except XpdfError as err:
            self._error("No se pudo modificar las páginas", str(err))
            return False
        except Exception:
            log.exception("Error en %s", cmd.label)
            self._error("No se pudo modificar las páginas", "Ha ocurrido un error inesperado; el documento no se ha modificado.")
            return False
        return True

    def rotate_pages(self, degrees: int, pages: list[int] | None = None) -> None:
        if self.doc is None:
            return
        sel = self._target_pages(pages)
        if sel and self._run_page_command(rotate_command(self.doc, sel, degrees)):  # type: ignore[arg-type]
            self.thumbs.select_pages(sel)

    def delete_pages(self, pages: list[int] | None = None) -> None:
        if self.doc is None:
            return
        sel = self._target_pages(pages)
        if not sel:
            return
        if self._run_page_command(delete_command(self.doc, sel)):  # type: ignore[arg-type]
            n = len(sel)
            self.show_status(
                f"{'Página borrada' if n == 1 else f'{n} páginas borradas'} · Deshacer: "
                + self.act_undo.shortcut().toString(QKeySequence.SequenceFormat.NativeText),
                6000,
            )
            keep = min(sel[0], self.doc.page_count - 1)
            self.thumbs.select_pages([keep])
            self.view.scroll_to_page(keep)

    def move_pages(self, pages: list[int], target: int) -> None:
        if self.doc is None or not pages:
            return
        cmd = move_command(self.doc, pages, target)
        if self._run_page_command(cmd):  # type: ignore[arg-type]
            moved = cmd.result or []
            self.thumbs.select_pages(moved)
            if moved:
                self.view.scroll_to_page(moved[0])

    def optimize(self) -> None:
        if self.doc is None:
            return
        self._finish_editing(commit=True)
        run_optimize_dialog(self, self.doc)

    # ---------------------------------------------------------------- estado
    def _update_state(self) -> None:
        has_doc = self.doc is not None
        self.stack.setCurrentIndex(1 if has_doc else 0)
        for a in (
            self.act_save, self.act_save_as, self.act_optimize, self.act_zoom_in,
            self.act_zoom_out, self.act_fit, self.act_actual,
            self.tools["edit_text"], self.tools["add_text"],
            self.act_rot_left, self.act_rot_right, self.act_delete_pages,
        ):
            a.setEnabled(has_doc)
        self.act_undo.setEnabled(self.history.can_undo)
        self.act_redo.setEnabled(self.history.can_redo)
        self.act_undo.setText(f"Deshacer {self.history.undo_label}".strip())
        self.act_redo.setText(f"Rehacer {self.history.redo_label}".strip())
        self.setWindowModified(has_doc and not self.history.is_clean)
        if self.doc is not None and self.doc.path is not None:
            self.setWindowTitle(f"{self.doc.path.name}[*] — xpdf")
            self.setWindowFilePath(str(self.doc.path))
        else:
            self.setWindowTitle("xpdf")
            self.setWindowFilePath("")
        if not has_doc:
            self.page_label.setText("")

    def _on_zoom_changed(self, zoom: float) -> None:
        self.zoom_label.setText(f"{round(zoom * 100)} %")

    def _on_page_changed(self, index: int) -> None:
        if self.doc is not None:
            self.page_label.setText(f"Página {index + 1} de {self.doc.page_count}")
        self.thumbs.set_current_page(index)

    def _toggle_sidebar(self) -> None:
        self.thumbs.setVisible(self.act_sidebar.isChecked())

    def _error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    # ---------------------------------------------------------------- eventos
    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ThemeChange):
            for a in self.actions():
                if isinstance(name := a.data(), str):
                    a.setIcon(icon(name))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        path = self._dropped_pdf(event)
        if path is None:
            return
        event.acceptProposedAction()
        name = Path(path).name
        if self.doc is None:
            self.welcome.set_drag_active(True, name)
        else:
            self.drop_overlay.show_for(name)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._end_drag_feedback()

    def _end_drag_feedback(self) -> None:
        self.welcome.set_drag_active(False)
        self.drop_overlay.hide()

    def dropEvent(self, event: QDropEvent) -> None:
        self._end_drag_feedback()
        path = self._dropped_pdf(event)
        if path is not None and self._maybe_save():
            event.acceptProposedAction()
            self.open_path(path)

    @staticmethod
    def _dropped_pdf(event: QDragEnterEvent | QDropEvent) -> str | None:
        md = event.mimeData()
        if not md.hasUrls():
            return None
        for url in md.urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                return url.toLocalFile()
        return None

    def closeEvent(self, event: QCloseEvent) -> None:
        self._finish_editing(commit=True)
        if not self._maybe_save():
            event.ignore()
            return
        self._settings.setValue("window/geometry", self.saveGeometry())
        if self.doc is not None:
            self.doc.close()
        event.accept()
