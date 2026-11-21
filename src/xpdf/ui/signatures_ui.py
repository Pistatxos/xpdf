"""Diálogos de firmas: gestor (certificados y rúbricas), dibujar, firmar y estado."""

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, QSettings, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from xpdf.core.signatures import (
    CertificateEntry,
    DrawnSignature,
    SignatureError,
    SignatureStatus,
    SignatureStore,
    clean_signature_image,
    read_certificate,
)
from xpdf.paths import assets_dir

log = logging.getLogger(__name__)

CERT_FILTER = "Certificados digitales (*.pfx *.p12)"
IMAGE_FILTER = "Imágenes (*.png *.jpg *.jpeg)"
INK = {"Azul": QColor(22, 45, 140), "Negro": QColor(20, 20, 20)}


def cert_text(c: CertificateEntry) -> str:
    when = c.expires.strftime("%d/%m/%Y")
    state = f"caducado el {when}" if c.expired else f"válido hasta {when}"
    name = c.label if c.label == c.subject else f"{c.label} · {c.subject}"
    return f"{name} ({state})"


def ask_password(parent: QWidget, cert_name: str, error: str = "") -> str | None:
    text = f"Contraseña del certificado «{cert_name}»:"
    if error:
        text = f"{error}\n\n{text}"
    pwd, ok = QInputDialog.getText(parent, "Contraseña del certificado", text, QLineEdit.EchoMode.Password)
    return pwd if ok else None


# ------------------------------------------------------------ dibujar firma
class SignaturePad(QWidget):
    """Lienzo para firmar con el ratón, el panel táctil o un lápiz."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.strokes: list[QPainterPath] = []
        self.color = INK["Azul"]
        self.pen_width = 2.6
        self.setMinimumSize(520, 200)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_StaticContents)

    def clear(self) -> None:
        self.strokes.clear()
        self.update()

    def is_empty(self) -> bool:
        return not self.strokes

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            path = QPainterPath(e.position())
            path.lineTo(e.position() + QPointF(0.01, 0.01))  # un punto también se ve
            self.strokes.append(path)
            self.update()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if e.buttons() & Qt.MouseButton.LeftButton and self.strokes:
            self.strokes[-1].lineTo(e.position())
            self.update()

    def _pen(self, scale: float = 1.0) -> QPen:
        pen = QPen(self.color, self.pen_width * scale)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return pen

    def paintEvent(self, e: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(255, 255, 255))
        p.setPen(QPen(QColor(0, 0, 0, 40), 1, Qt.PenStyle.DashLine))
        base = int(self.height() * 0.72)
        p.drawLine(24, base, self.width() - 24, base)  # línea guía
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(self._pen())
        for s in self.strokes:
            p.drawPath(s)
        p.end()

    def to_png(self, scale: float = 3.0) -> bytes:
        """La firma recortada, con fondo transparente y a alta resolución."""
        bounds = QRectF()
        for s in self.strokes:
            bounds = bounds.united(s.boundingRect())
        m = self.pen_width * 2
        bounds.adjust(-m, -m, m, m)
        img = QImage(
            max(1, int(bounds.width() * scale)), max(1, int(bounds.height() * scale)),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(scale, scale)
        p.translate(-bounds.topLeft())
        p.setPen(self._pen())
        for s in self.strokes:
            p.drawPath(s)
        p.end()
        return _png_bytes(img)


def _png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")  # type: ignore[call-overload]  # (los stubs piden bytes; PySide quiere str)
    buf.close()
    return bytes(data.data())


class DrawSignatureDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Dibujar firma")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Firma con el ratón, el panel táctil o un lápiz:"))
        self.pad = SignaturePad(self)
        lay.addWidget(self.pad, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Color:"))
        self.color = QComboBox(self)
        self.color.addItems(list(INK))
        self.color.currentTextChanged.connect(self._set_color)
        row.addWidget(self.color)
        clear = QPushButton("Borrar", self)
        clear.clicked.connect(self.pad.clear)
        row.addWidget(clear)
        row.addStretch(1)
        row.addWidget(QLabel("Nombre:"))
        self.name = QLineEdit(self)
        self.name.setPlaceholderText("p. ej. Mi rúbrica")
        row.addWidget(self.name)
        lay.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _set_color(self, name: str) -> None:
        self.pad.color = INK[name]
        self.pad.update()

    def _accept(self) -> None:
        if self.pad.is_empty():
            QMessageBox.information(self, "Dibujar firma", "Dibuja tu firma en el recuadro blanco.")
            return
        self.accept()

    def label(self) -> str:
        return self.name.text().strip() or "Mi firma"


# ----------------------------------------------------------------- gestor
class SignaturesDialog(QDialog):
    """Firma → Gestionar firmas: certificados digitales y firmas dibujadas."""

    def __init__(self, store: SignatureStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Gestionar firmas")
        self.resize(620, 520)
        lay = QVBoxLayout(self)

        box = QGroupBox("Certificados digitales (.pfx / .p12)", self)
        bl = QVBoxLayout(box)
        self.certs = QListWidget(box)
        bl.addWidget(self.certs)
        row = QHBoxLayout()
        add = QPushButton("Añadir certificado…", box)
        add.clicked.connect(self.add_certificate)
        self.cert_rename = QPushButton("Renombrar", box)
        self.cert_rename.clicked.connect(self._rename_cert)
        self.cert_remove = QPushButton("Quitar", box)
        self.cert_remove.clicked.connect(self._remove_cert)
        row.addWidget(add)
        row.addStretch(1)
        row.addWidget(self.cert_rename)
        row.addWidget(self.cert_remove)
        bl.addLayout(row)
        note = QLabel(
            "La contraseña no se guarda: se pide cada vez que firmas. "
            "xpdf solo recuerda dónde está el archivo del certificado.", box
        )
        note.setWordWrap(True)
        note.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        bl.addWidget(note)
        lay.addWidget(box)

        box = QGroupBox("Firmas dibujadas (rúbricas)", self)
        bl = QVBoxLayout(box)
        self.drawn = QListWidget(box)
        self.drawn.setIconSize(QSize(120, 40))
        bl.addWidget(self.drawn)
        row = QHBoxLayout()
        draw = QPushButton("Dibujar…", box)
        draw.clicked.connect(self.draw_signature)
        imp = QPushButton("Importar imagen…", box)
        imp.setToolTip("Una foto o escaneo de tu firma sobre papel blanco: se quita el fondo")
        imp.clicked.connect(self._import_image)
        self.drawn_rename = QPushButton("Renombrar", box)
        self.drawn_rename.clicked.connect(self._rename_drawn)
        self.drawn_remove = QPushButton("Quitar", box)
        self.drawn_remove.clicked.connect(self._remove_drawn)
        row.addWidget(draw)
        row.addWidget(imp)
        row.addStretch(1)
        row.addWidget(self.drawn_rename)
        row.addWidget(self.drawn_remove)
        bl.addLayout(row)
        lay.addWidget(box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        help_btn = buttons.addButton("Cómo firmar", QDialogButtonBox.ButtonRole.HelpRole)
        help_btn.clicked.connect(lambda: show_help(self))
        lay.addWidget(buttons)

        self.certs.currentRowChanged.connect(lambda _: self._update_buttons())
        self.drawn.currentRowChanged.connect(lambda _: self._update_buttons())
        self.refresh()

    # ---------------------------------------------------------------- listas
    def refresh(self) -> None:
        self.certs.clear()
        for c in self.store.certificates:
            it = QListWidgetItem(cert_text(c))
            it.setToolTip(f"Titular: {c.subject}\nEmisor: {c.issuer}\nArchivo: {c.path}")
            if c.expired or not Path(c.path).is_file():
                it.setForeground(QColor(200, 60, 60))
                if not Path(c.path).is_file():
                    it.setText(it.text() + " — archivo no encontrado")
            it.setData(Qt.ItemDataRole.UserRole, c.id)
            self.certs.addItem(it)
        self.drawn.clear()
        for d in self.store.drawn:
            it = QListWidgetItem(QIcon(_preview(self.store.drawn_path(d))), d.label)
            it.setData(Qt.ItemDataRole.UserRole, d.id)
            self.drawn.addItem(it)
        self._update_buttons()

    def _update_buttons(self) -> None:
        c = self.certs.currentRow() >= 0
        d = self.drawn.currentRow() >= 0
        self.cert_rename.setEnabled(c)
        self.cert_remove.setEnabled(c)
        self.drawn_rename.setEnabled(d)
        self.drawn_remove.setEnabled(d)

    def _current_cert(self) -> CertificateEntry | None:
        row = self.certs.currentRow()
        return self.store.certificates[row] if 0 <= row < len(self.store.certificates) else None

    def _current_drawn(self) -> DrawnSignature | None:
        row = self.drawn.currentRow()
        return self.store.drawn[row] if 0 <= row < len(self.store.drawn) else None

    # ---------------------------------------------------------- certificados
    def add_certificate(self) -> CertificateEntry | None:
        settings = QSettings()
        start = str(settings.value("paths/last_cert_dir", str(Path.home())))
        name, _ = QFileDialog.getOpenFileName(self, "Añadir certificado digital", start, CERT_FILTER)
        if not name:
            return None
        path = Path(name)
        settings.setValue("paths/last_cert_dir", str(path.parent))
        if any(Path(c.path) == path.resolve() for c in self.store.certificates):
            QMessageBox.information(self, "Añadir certificado", "Ese certificado ya está en la lista.")
            return None
        error = ""
        while True:
            pwd = ask_password(self, path.name, error)
            if pwd is None:
                return None
            try:
                info = read_certificate(path, pwd)
                break
            except SignatureError as err:
                if "Contraseña" not in str(err):
                    QMessageBox.warning(self, "Añadir certificado", str(err))
                    return None
                error = str(err)
        label, ok = QInputDialog.getText(
            self, "Añadir certificado",
            f"Titular: {info.subject}\nVálido hasta: {info.not_after.strftime('%d/%m/%Y')}\n\n"
            "Nombre para reconocerlo en la lista:",
            text=info.subject,
        )
        if not ok:
            return None
        try:
            entry = self.store.add_certificate(path, pwd, label.strip() or info.subject)
        except SignatureError as err:
            QMessageBox.warning(self, "Añadir certificado", str(err))
            return None
        self.refresh()
        self.certs.setCurrentRow(len(self.store.certificates) - 1)
        return entry

    def _rename_cert(self) -> None:
        c = self._current_cert()
        if c is None:
            return
        label, ok = QInputDialog.getText(self, "Renombrar", "Nombre del certificado:", text=c.label)
        if ok and label.strip():
            c.label = label.strip()
            self.store.save()
            self.refresh()

    def _remove_cert(self) -> None:
        c = self._current_cert()
        if c is None:
            return
        res = QMessageBox.question(
            self, "Quitar certificado",
            f"¿Quitar «{c.label}» de la lista?\n\nEl archivo del certificado no se borra.",
        )
        if res == QMessageBox.StandardButton.Yes:
            self.store.remove_certificate(c.id)
            self.refresh()

    # ------------------------------------------------------- firmas dibujadas
    def draw_signature(self) -> DrawnSignature | None:
        dlg = DrawSignatureDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        sig = self.store.add_drawn(dlg.pad.to_png(), dlg.label())
        self.refresh()
        self.drawn.setCurrentRow(len(self.store.drawn) - 1)
        return sig

    def _import_image(self) -> None:
        settings = QSettings()
        start = str(settings.value("paths/last_image_dir", str(Path.home())))
        name, _ = QFileDialog.getOpenFileName(self, "Importar imagen de firma", start, IMAGE_FILTER)
        if not name:
            return
        settings.setValue("paths/last_image_dir", str(Path(name).parent))
        try:
            png = clean_signature_image(Path(name))
        except SignatureError as err:
            QMessageBox.warning(self, "Importar imagen", str(err))
            return
        label, ok = QInputDialog.getText(self, "Importar imagen", "Nombre de la firma:", text="Mi firma")
        if not ok:
            return
        self.store.add_drawn(png, label.strip() or "Mi firma")
        self.refresh()
        self.drawn.setCurrentRow(len(self.store.drawn) - 1)

    def _rename_drawn(self) -> None:
        d = self._current_drawn()
        if d is None:
            return
        label, ok = QInputDialog.getText(self, "Renombrar", "Nombre de la firma:", text=d.label)
        if ok and label.strip():
            d.label = label.strip()
            self.store.save()
            self.refresh()

    def _remove_drawn(self) -> None:
        d = self._current_drawn()
        if d is None:
            return
        res = QMessageBox.question(self, "Quitar firma", f"¿Borrar la firma dibujada «{d.label}»?")
        if res == QMessageBox.StandardButton.Yes:
            self.store.remove_drawn(d.id)
            self.refresh()


def _preview(path: Path) -> QPixmap:
    """Miniatura de la rúbrica sobre blanco (en tema oscuro la tinta no se vería)."""
    src = QPixmap(str(path))
    out = QPixmap(240, 80)
    out.fill(QColor(255, 255, 255))
    if not src.isNull():
        scaled = src.scaled(232, 72, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        p = QPainter(out)
        p.drawPixmap((240 - scaled.width()) // 2, (80 - scaled.height()) // 2, scaled)
        p.end()
    return out


# ------------------------------------------------------------------ firmar
class SignDialog(QDialog):
    """Datos para firmar: certificado, contraseña, rúbrica y motivo."""

    def __init__(self, store: SignatureStore, cert_id: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Firmar con certificado digital")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.cert = QComboBox(self)
        for c in store.certificates:
            self.cert.addItem(cert_text(c), c.id)
        settings = QSettings()
        wanted = cert_id or str(settings.value("sign/last_cert", ""))
        if (i := self.cert.findData(wanted)) >= 0:
            self.cert.setCurrentIndex(i)
        form.addRow("Certificado:", self.cert)
        self.password = QLineEdit(self)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Contraseña:", self.password)
        self.rubric = QComboBox(self)
        self.rubric.setIconSize(QSize(90, 30))
        self.rubric.addItem("Sin rúbrica (solo texto)", "")
        for d in store.drawn:
            self.rubric.addItem(QIcon(_preview(store.drawn_path(d))), d.label, d.id)
        last_rubric = str(settings.value("sign/last_rubric", ""))
        if (i := self.rubric.findData(last_rubric)) >= 0:
            self.rubric.setCurrentIndex(i)
        form.addRow("Rúbrica:", self.rubric)
        self.reason = QLineEdit(self)
        self.reason.setPlaceholderText("Opcional, p. ej. «Conforme»")
        form.addRow("Motivo:", self.reason)
        self.location = QLineEdit(self)
        self.location.setPlaceholderText("Opcional, p. ej. «Barcelona»")
        self.location.setText(str(settings.value("sign/last_location", "")))
        form.addRow("Lugar:", self.location)
        lay.addLayout(form)
        self.error = QLabel("", self)
        self.error.setStyleSheet("color: #c83c3c")
        self.error.setWordWrap(True)
        self.error.hide()
        lay.addWidget(self.error)
        note = QLabel("Se guardará una copia firmada del documento; el original no se modifica.", self)
        note.setWordWrap(True)
        note.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        lay.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        ok = buttons.addButton("Firmar…", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setDefault(True)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.password.setFocus()

    def entry(self) -> CertificateEntry:
        cid = self.cert.currentData()
        return next(c for c in self.store.certificates if c.id == cid)

    def rubric_path(self) -> Path | None:
        did = self.rubric.currentData()
        for d in self.store.drawn:
            if d.id == did:
                return self.store.drawn_path(d)
        return None

    def _accept(self) -> None:
        entry = self.entry()
        try:
            info = read_certificate(Path(entry.path), self.password.text())
        except SignatureError as err:
            self.error.setText(str(err))
            self.error.show()
            self.password.selectAll()
            self.password.setFocus()
            return
        if info.not_after < dt.datetime.now(dt.timezone.utc):
            res = QMessageBox.question(
                self, "Certificado caducado",
                f"El certificado caducó el {info.not_after.strftime('%d/%m/%Y')}. "
                "La firma no será válida. ¿Firmar igualmente?",
            )
            if res != QMessageBox.StandardButton.Yes:
                return
        settings = QSettings()
        settings.setValue("sign/last_cert", entry.id)
        settings.setValue("sign/last_rubric", self.rubric.currentData())
        settings.setValue("sign/last_location", self.location.text().strip())
        self.accept()


# ------------------------------------------------------------------ estado
def show_signature_status(parent: QWidget, statuses: list[SignatureStatus], modified: bool) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle("Firmas del documento")
    if not statuses:
        box.setText("Este documento no tiene firmas digitales.")
        box.exec()
        return
    lines = []
    for s in statuses:
        mark = "✅" if s.intact else "❌"
        state = "íntegra: el documento no ha cambiado desde que se firmó" if s.intact else (
            "el documento se ha modificado después de firmar"
        )
        when = f" · {s.signed_at}" if s.signed_at else ""
        lines.append(f"{mark} <b>{s.signer}</b>{when}<br>&nbsp;&nbsp;&nbsp;&nbsp;{state}")
    text = "<br><br>".join(lines)
    if modified:
        text += "<br><br><i>Se muestra el estado del archivo guardado, sin tus cambios pendientes.</i>"
    box.setText(text)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setInformativeText(
        "xpdf comprueba que el documento no se ha alterado. No comprueba si la entidad "
        "emisora del certificado es de confianza: para eso usa Adobe Acrobat Reader o "
        "el servicio VALIDe del Gobierno."
    )
    box.exec()


# ------------------------------------------------------------------- ayuda
def help_text() -> str:
    for p in (assets_dir() / "help" / "firma.md", Path(__file__).resolve().parents[3] / "docs" / "firma.md"):
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return "# Cómo firmar\n\nNo se encuentra el archivo de ayuda."


def show_help(parent: QWidget) -> None:
    dlg = QDialog(parent)
    dlg.setWindowTitle("Cómo firmar documentos")
    dlg.resize(640, 640)
    lay = QVBoxLayout(dlg)
    view = QTextBrowser(dlg)
    view.setOpenExternalLinks(True)
    text = help_text()
    if sys.platform == "darwin":
        text = text.replace("Ctrl+", "⌘")
    view.setMarkdown(text)
    lay.addWidget(view)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
    buttons.rejected.connect(dlg.reject)
    lay.addWidget(buttons)
    dlg.exec()
