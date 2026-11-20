"""Interfaz de actualizaciones: comprobación al arrancar, diálogo y descarga."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QWidget

from xpdf import GITHUB_REPO, __version__, updater
from xpdf.updater import Release, UpdateError

log = logging.getLogger(__name__)

CHECK_EVERY_S = 20 * 3600  # como mucho una comprobación automática al día


class _Worker(QObject):
    done = Signal(object)  # Release | None
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, job: Callable[["_Worker"], object]) -> None:
        super().__init__()
        self._job = job

    def run(self) -> None:
        try:
            self.done.emit(self._job(self))
        except UpdateError as err:
            self.failed.emit(str(err))
        except Exception:
            log.exception("Error en la actualización")
            self.failed.emit("Error inesperado al actualizar.")


def _run_in_thread(owner: QObject, job: Callable[[_Worker], object]) -> _Worker:
    thread = QThread(owner)
    worker = _Worker(job)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.done.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return worker


class UpdateController(QObject):
    """Busca e instala actualizaciones para la ventana `window`.

    `before_install` debe devolver False si el usuario cancela (p. ej. al
    preguntar por cambios sin guardar).
    """

    def __init__(self, window: QWidget, before_install: Callable[[], bool]) -> None:
        super().__init__(window)
        self.window = window
        self.before_install = before_install
        self.settings = QSettings()
        self._busy = False
        self._workers: list[_Worker] = []  # mantener vivas las referencias
        # Contexto de la operación en curso. Las señales del hilo de trabajo se
        # conectan a métodos de este objeto (que vive en el hilo de la interfaz):
        # así Qt las entrega en ese hilo. Nunca a lambdas, que podrían ejecutarse
        # en el hilo de trabajo y abrir diálogos desde allí.
        self._interactive = False
        self._dlg: QProgressDialog | None = None
        self._cancelled = False

    # ----------------------------------------------------------- comprobación
    def schedule_startup_check(self, delay_ms: int = 4000) -> None:
        if not updater.can_self_update():
            return
        last = float(str(self.settings.value("updates/last_check", 0) or 0))
        if time.time() - last < CHECK_EVERY_S:
            return
        QTimer.singleShot(delay_ms, lambda: self.check(interactive=False))

    def check(self, interactive: bool) -> None:
        if self._busy:
            return
        if not updater.can_self_update():
            if interactive:
                QMessageBox.information(
                    self.window,
                    "Actualizaciones",
                    f"Tienes xpdf {__version__}.\n\nLas actualizaciones automáticas solo "
                    "funcionan en la versión instalada (Windows, o macOS con Apple Silicon).",
                )
            return
        self._busy = True
        self._interactive = interactive
        worker = _run_in_thread(self, lambda w: updater.fetch_latest(GITHUB_REPO))
        self._workers.append(worker)
        worker.done.connect(self._checked)
        worker.failed.connect(self._check_failed)

    def _check_failed(self, message: str) -> None:
        self._busy = False
        if self._interactive:
            QMessageBox.warning(self.window, "Actualizaciones", message)

    def _checked(self, rel: Release | None) -> None:
        self._busy = False
        interactive = self._interactive
        self.settings.setValue("updates/last_check", time.time())
        if rel is None or not updater.is_newer(rel.version):
            if interactive:
                QMessageBox.information(
                    self.window, "Actualizaciones", f"Tienes la última versión de xpdf ({__version__})."
                )
            return
        if not interactive and self.settings.value("updates/skip") == rel.version:
            return
        self._offer(rel)

    # ------------------------------------------------------------ instalación
    def _offer(self, rel: Release) -> None:
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Actualización disponible")
        box.setText(f"Hay una versión nueva de xpdf: <b>{rel.version}</b> (tienes la {__version__}).")
        box.setInformativeText("¿Quieres actualizar ahora? xpdf se cerrará y volverá a abrirse.")
        if rel.notes.strip():
            box.setDetailedText(rel.notes.strip())
        now = box.addButton("Actualizar ahora", QMessageBox.ButtonRole.AcceptRole)
        later = box.addButton("Más tarde", QMessageBox.ButtonRole.RejectRole)
        skip = box.addButton("Omitir esta versión", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(now)
        box.exec()
        clicked = box.clickedButton()
        if clicked is skip:
            self.settings.setValue("updates/skip", rel.version)
        elif clicked is now:
            self._install(rel)
        del later

    def _install(self, rel: Release) -> None:
        if not self.before_install():
            return
        dlg = QProgressDialog("Descargando la actualización…", "Cancelar", 0, 100, self.window)
        dlg.setWindowTitle("Actualizando xpdf")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setValue(0)
        self._dlg = dlg
        self._cancelled = False
        dlg.canceled.connect(self._cancel)

        def job(w: _Worker) -> object:
            def progress(done: int, total: int) -> None:
                if self._cancelled:  # lectura de un bool: segura entre hilos
                    raise UpdateError("Actualización cancelada.")
                w.progress.emit(done, total)

            return updater.download(rel, updater.download_dir(), progress)

        self._busy = True
        worker = _run_in_thread(self, job)
        self._workers.append(worker)
        worker.progress.connect(self._progress)
        worker.done.connect(self._downloaded)
        worker.failed.connect(self._install_failed)

    def _cancel(self) -> None:
        self._cancelled = True

    def _progress(self, done: int, total: int) -> None:
        if self._dlg is None:
            return
        if total:
            self._dlg.setValue(int(done * 100 / total))
        else:
            self._dlg.setRange(0, 0)

    def _install_failed(self, message: str) -> None:
        self._busy = False
        if self._dlg is not None:
            self._dlg.close()
        if not self._cancelled:
            QMessageBox.warning(self.window, "Actualización", message)

    def _downloaded(self, path: object) -> None:
        self._busy = False
        dlg = self._dlg
        assert dlg is not None
        dlg.setLabelText("Instalando…")
        try:
            updater.apply_update(path)  # type: ignore[arg-type]
        except UpdateError as err:
            dlg.close()
            QMessageBox.warning(self.window, "Actualización", str(err))
            return
        dlg.close()
        log.info("Actualización lanzada; cerrando xpdf")
        QApplication.quit()


def show_about(parent: QWidget) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle("Acerca de xpdf")
    box.setIconPixmap(parent.windowIcon().pixmap(64, 64))
    repo = GITHUB_REPO if "TU-USUARIO" not in GITHUB_REPO else None
    link = f'<br><a href="https://github.com/{repo}">github.com/{repo}</a>' if repo else ""
    box.setText(
        f"<b>xpdf {__version__}</b><br>Editor de PDF de escritorio, minimalista."
        f"<br><br>Software libre bajo licencia AGPL-3.0.{link}"
    )
    box.setTextFormat(Qt.TextFormat.RichText)
    box.exec()


def open_releases_page() -> None:
    if "TU-USUARIO" not in GITHUB_REPO:
        QDesktopServices.openUrl(QUrl(f"https://github.com/{GITHUB_REPO}/releases"))
