"""Punto de entrada de xpdf."""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import QEvent, QSize
from PySide6.QtGui import QFileOpenEvent, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from xpdf import __version__
from xpdf.logging_setup import setup_logging
from xpdf.paths import assets_dir, log_file

log = logging.getLogger("xpdf")


class XpdfApp(QApplication):
    """Captura la apertura de archivos desde Finder («Abrir con», doble clic) en macOS."""

    def __init__(self, argv: list[str]) -> None:
        super().__init__(argv)
        self.pending_open: str | None = None
        self.window: object | None = None

    def event(self, e: QEvent) -> bool:
        if e.type() == QEvent.Type.FileOpen and isinstance(e, QFileOpenEvent):
            path = e.file()
            from xpdf.ui.main_window import MainWindow

            if isinstance(self.window, MainWindow):
                if self.window._maybe_save():
                    self.window.open_path(path)
            else:
                self.pending_open = path
            return True
        return super().event(e)


def _excepthook(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    log.error("Error no controlado:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
    if QApplication.instance() is not None:
        QMessageBox.critical(
            None,
            "Error inesperado",
            "Ha ocurrido un error inesperado. Puede que la última acción no se haya completado.\n\n"
            f"Los detalles se han guardado en:\n{log_file()}",
        )


def main() -> int:
    for arg in sys.argv[1:]:
        if arg.startswith("--selftest="):
            from xpdf.selftest import run

            return run(Path(arg.split("=", 1)[1]))
    debug = "--debug" in sys.argv
    setup_logging(debug)
    log.info("xpdf %s arrancando", __version__)
    sys.excepthook = _excepthook

    app = XpdfApp(sys.argv)
    app.setApplicationName("xpdf")
    app.setOrganizationName("xpdf")
    app.setApplicationVersion(__version__)
    # Dos dibujos: «xpdf» en grande y solo la «x» en tamaños pequeños (barra de título).
    app_icon = QIcon(str(assets_dir() / "icons" / "app.png"))
    app_icon.addFile(str(assets_dir() / "icons" / "app-small.png"), QSize(32, 32))
    app_icon.addFile(str(assets_dir() / "icons" / "app-small.png"), QSize(16, 16))
    app.setWindowIcon(app_icon)
    if sys.platform == "win32":
        # Windows agrupa por este identificador en la barra de tareas (y usa su icono).
        import ctypes

        # getattr: «windll» solo existe en Windows (así mypy acepta el código en todas).
        getattr(ctypes, "windll").shell32.SetCurrentProcessExplicitAppUserModelID("xpdf.xpdf")
    if sys.platform == "win32" and app.style().name().lower() == "windowsvista":
        # El estilo clásico de Windows no tiene modo oscuro; Fusion sí sigue al sistema.
        app.setStyle("Fusion")

    from xpdf.ui.main_window import MainWindow

    win = MainWindow()
    app.window = win
    win.show()

    files = [a for a in sys.argv[1:] if not a.startswith("--")]
    target = files[0] if files else app.pending_open
    if target:
        win.open_path(target)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

