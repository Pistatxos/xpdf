"""Diálogo «Reducir tamaño…»: guarda una copia más ligera del documento.

La optimización se ejecuta en un QThread sobre una copia independiente del
documento (`optimize.snapshot` se toma en el hilo principal), de modo que la
interfaz sigue respondiendo y el documento abierto no se toca.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QEventLoop, QObject, QSettings, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from xpdf.core.document import Document
from xpdf.core.errors import XpdfError
from xpdf.core.optimize import (
    Level,
    OptimizeCancelled,
    OptimizeResult,
    Snapshot,
    format_result,
    format_size,
    optimize_snapshot,
    snapshot,
)

log = logging.getLogger(__name__)

PDF_FILTER = "Documentos PDF (*.pdf)"
_SETTINGS_KEY = "optimize/level"

_LEVELS: tuple[tuple[Level, str, str], ...] = (
    (
        Level.LOSSLESS,
        "Sin pérdida",
        "Limpia y comprime la estructura. El aspecto no cambia en absoluto.",
    ),
    (
        Level.BALANCED,
        "Equilibrado (150 ppp)",
        "Recomprime las imágenes grandes. Ideal para pantalla y para imprimir en casa.",
    ),
    (
        Level.SMALL,
        "Máxima reducción (96 ppp)",
        "El archivo más pequeño, para enviar por correo. Las fotos pierden detalle.",
    ),
)


class _Worker(QObject):
    progressed = Signal(int)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, snap: Snapshot, target: Path, level: Level) -> None:
        super().__init__()
        self._snap = snap
        self._target = target
        self._level = level
        self.cancel_event = threading.Event()

    def _progress(self, fraction: float) -> None:
        if self.cancel_event.is_set():
            raise OptimizeCancelled()
        self.progressed.emit(int(fraction * 100))

    @Slot()
    def run(self) -> None:
        try:
            result = optimize_snapshot(self._snap, self._target, self._level, self._progress)
        except OptimizeCancelled:
            self.cancelled.emit()
        except XpdfError as exc:
            self.failed.emit(str(exc))
        except Exception:  # nunca mostrar trazas al usuario
            log.exception("Error inesperado reduciendo tamaño")
            self.failed.emit("Error inesperado al reducir el tamaño. Consulta el registro.")
        else:
            self.succeeded.emit(result)


class OptimizeDialog(QDialog):
    def __init__(self, parent: QWidget | None, doc: Document) -> None:
        super().__init__(parent)
        self.setWindowTitle("Reducir tamaño")
        self.snap = snapshot(doc)
        self.target: Path | None = None
        self.result_: OptimizeResult | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        size = QLabel(f"Tamaño actual: <b>{format_size(self.snap.size)}</b>", self)
        layout.addWidget(size)
        if self.snap.has_signatures:
            warn = QLabel(
                "Este PDF tiene firmas digitales: dejarán de ser válidas en la copia reducida.",
                self,
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #c0392b;")
            layout.addWidget(warn)
        layout.addSpacing(6)

        self.group = QButtonGroup(self)
        saved = QSettings().value(_SETTINGS_KEY, Level.BALANCED.value)
        for level, title, hint in _LEVELS:
            radio = QRadioButton(title, self)
            radio.setProperty("level", level.value)
            radio.setChecked(level.value == saved)
            self.group.addButton(radio)
            layout.addWidget(radio)
            note = QLabel(hint, self)
            note.setWordWrap(True)
            note.setContentsMargins(22, 0, 0, 6)
            note.setEnabled(False)  # gris, según el tema
            layout.addWidget(note)
        if self.group.checkedButton() is None:
            self.group.buttons()[1].setChecked(True)

        buttons = QDialogButtonBox(self)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        save: QPushButton = buttons.addButton("Guardar como…", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setDefault(True)
        buttons.accepted.connect(self._choose_and_run)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(420)

    @property
    def level(self) -> Level:
        button = self.group.checkedButton()
        return Level(button.property("level")) if button else Level.BALANCED

    # -------------------------------------------------------------- acciones
    def _ask_target(self) -> Path | None:
        src = self.snap.source_path
        folder = src.parent if src is not None else Path.home()
        suggested = str(folder / f"{self.snap.name} (reducido).pdf")
        while True:
            path, _ = QFileDialog.getSaveFileName(self, "Guardar copia reducida", suggested, PDF_FILTER)
            if not path:
                return None
            target = Path(path)
            if target.suffix.lower() != ".pdf":
                target = target.with_name(target.name + ".pdf")
            if src is not None and target.resolve() == src.resolve():
                QMessageBox.warning(
                    self,
                    "Reducir tamaño",
                    "La copia reducida no puede sustituir al original. Elige otro nombre.",
                )
                suggested = str(target.with_name(f"{self.snap.name} (reducido).pdf"))
                continue
            return target

    def _choose_and_run(self) -> None:
        target = self._ask_target()
        if target is None:
            return
        QSettings().setValue(_SETTINGS_KEY, self.level.value)
        result = self.optimize_to(target)
        if result is None:
            return
        if result.reduced:
            msg = f"Copia guardada como «{target.name}».\n\n{format_result(result)}"
        else:
            msg = (
                f"Copia guardada como «{target.name}».\n\n"
                "No se ha podido reducir más: el documento ya estaba optimizado, "
                "así que la copia es idéntica al original."
            )
        QMessageBox.information(self, "Reducir tamaño", msg)
        self.accept()

    def optimize_to(self, target: Path) -> OptimizeResult | None:
        """Ejecuta la optimización con barra de progreso. None si falla o se cancela."""
        progress = QProgressDialog("Reduciendo tamaño…", "Cancelar", 0, 100, self)
        progress.setWindowTitle("Reducir tamaño")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)

        thread = QThread(self)
        worker = _Worker(self.snap, target, self.level)
        worker.moveToThread(thread)
        self._loop = QEventLoop(self)
        self._outcome: dict[str, object] = {}
        # Se conectan a métodos del diálogo (hilo principal): conexión en cola.
        worker.progressed.connect(progress.setValue)
        worker.succeeded.connect(self._on_succeeded)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        progress.canceled.connect(worker.cancel_event.set)
        thread.started.connect(worker.run)
        thread.start()
        self._loop.exec()
        thread.quit()
        thread.wait()
        progress.close()
        worker.deleteLater()
        thread.deleteLater()

        outcome = self._outcome
        if "error" in outcome:
            QMessageBox.warning(self, "Reducir tamaño", str(outcome["error"]))
            return None
        result = outcome.get("ok")
        self.result_ = result if isinstance(result, OptimizeResult) else None
        return self.result_

    @Slot(object)
    def _on_succeeded(self, result: object) -> None:
        self._outcome["ok"] = result
        self._loop.quit()

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self._outcome["error"] = message
        self._loop.quit()

    @Slot()
    def _on_cancelled(self) -> None:
        self._outcome["cancelled"] = True
        self._loop.quit()


def run_optimize_dialog(parent: QWidget, doc: Document) -> None:
    """Muestra el diálogo «Reducir tamaño» para `doc`. Muestra los errores al usuario."""
    try:
        dialog = OptimizeDialog(parent, doc)
    except XpdfError as exc:
        QMessageBox.warning(parent, "Reducir tamaño", str(exc))
        return
    dialog.exec()
