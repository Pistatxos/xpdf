"""Firmas: certificados digitales (PAdES con pyHanko) y firmas dibujadas.

- Los certificados se guardan como *referencia* al archivo `.pfx`/`.p12` (nunca
  la contraseña, que se pide al firmar). Puede haber varios: cada persona que
  usa el ordenador elige el suyo.
- Las firmas dibujadas son PNG con fondo transparente en la carpeta de datos.
- Firmar produce un archivo nuevo. Si el PDF ya estaba firmado, la firma nueva
  se añade al final (actualización incremental) para no invalidar las otras.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf

from xpdf.core.errors import XpdfError
from xpdf.paths import user_data_dir

log = logging.getLogger(__name__)

CERT_SUFFIXES = (".pfx", ".p12")


class SignatureError(XpdfError):
    """Error de firma con mensaje para el usuario."""


# ------------------------------------------------------------------ almacén
@dataclass(slots=True)
class CertificateEntry:
    id: str
    label: str  # nombre que ve el usuario
    path: str  # ruta del .pfx/.p12
    subject: str  # titular (CN)
    issuer: str
    not_after: str  # ISO 8601

    @property
    def expires(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.not_after)

    @property
    def expired(self) -> bool:
        return self.expires < dt.datetime.now(dt.timezone.utc)


@dataclass(slots=True)
class DrawnSignature:
    id: str
    label: str
    file: str  # PNG dentro de la carpeta de firmas


@dataclass(slots=True)
class SignatureStore:
    certificates: list[CertificateEntry] = field(default_factory=list)
    drawn: list[DrawnSignature] = field(default_factory=list)
    folder: Path = field(default_factory=lambda: user_data_dir() / "firmas")

    # ---------------------------------------------------------- persistencia
    @classmethod
    def load(cls, folder: Path | None = None) -> SignatureStore:
        folder = folder or user_data_dir() / "firmas"
        folder.mkdir(parents=True, exist_ok=True)
        store = cls(folder=folder)
        index = folder / "firmas.json"
        if index.is_file():
            try:
                data = json.loads(index.read_text(encoding="utf-8"))
                store.certificates = [CertificateEntry(**c) for c in data.get("certificates", [])]
                store.drawn = [DrawnSignature(**d) for d in data.get("drawn", [])]
            except (ValueError, TypeError):
                log.exception("Índice de firmas dañado; se ignora")
        return store

    def save(self) -> None:
        data = {
            "certificates": [asdict(c) for c in self.certificates],
            "drawn": [asdict(d) for d in self.drawn],
        }
        tmp = self.folder / "firmas.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.folder / "firmas.json")

    # ----------------------------------------------------------- certificados
    def add_certificate(self, path: Path, password: str, label: str | None = None) -> CertificateEntry:
        info = read_certificate(path, password)
        entry = CertificateEntry(
            id=uuid.uuid4().hex[:12],
            label=label or info.subject,
            path=str(path.resolve()),
            subject=info.subject,
            issuer=info.issuer,
            not_after=info.not_after.isoformat(),
        )
        self.certificates.append(entry)
        self.save()
        return entry

    def remove_certificate(self, entry_id: str) -> None:
        self.certificates = [c for c in self.certificates if c.id != entry_id]
        self.save()

    # ------------------------------------------------------- firmas dibujadas
    def add_drawn(self, png: bytes, label: str) -> DrawnSignature:
        sig = DrawnSignature(uuid.uuid4().hex[:12], label, "")
        sig.file = f"{sig.id}.png"
        (self.folder / sig.file).write_bytes(png)
        self.drawn.append(sig)
        self.save()
        return sig

    def remove_drawn(self, sig_id: str) -> None:
        for d in self.drawn:
            if d.id == sig_id:
                (self.folder / d.file).unlink(missing_ok=True)
        self.drawn = [d for d in self.drawn if d.id != sig_id]
        self.save()

    def drawn_path(self, sig: DrawnSignature) -> Path:
        return self.folder / sig.file


# ------------------------------------------------------- imagen de la rúbrica
#: Ancho máximo (px) de una rúbrica importada: de sobra para imprimir nítida.
MAX_SIGNATURE_PX = 1200


def clean_signature_image(path: Path) -> bytes:
    """PNG de una rúbrica escaneada o fotografiada: fondo claro transparente y
    recortada a la tinta. El trazo conserva su color."""
    try:
        pix = pymupdf.Pixmap(str(path))
    except Exception as exc:
        raise SignatureError(f"No se pudo leer la imagen «{path.name}».") from exc
    if pix.colorspace is None or pix.colorspace.n != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)  # sin alfa: se recalcula abajo
    while pix.width > MAX_SIGNATURE_PX:
        pix.shrink(1)  # mitad de tamaño
    w, h = pix.width, pix.height
    src = pix.samples
    stride = pix.stride
    out = bytearray(w * h * 4)
    xs: list[int] = []
    ys: list[int] = []
    for y in range(h):
        row = y * stride
        o = y * w * 4
        found = False
        for x in range(w):
            i = row + 3 * x
            r, g, b = src[i], src[i + 1], src[i + 2]
            dark = 255 - min(r, g, b)
            # Fondo (papel) transparente; trazos opacos con borde suave.
            a = 0 if dark < 40 else min(255, (dark - 40) * 3)
            j = o + 4 * x
            out[j : j + 4] = bytes((r, g, b, a))
            if a > 60:
                found = True
                xs.append(x)
        if found:
            ys.append(y)
    if not ys:
        raise SignatureError("No se ha encontrado ninguna firma en la imagen (¿está en blanco?).")
    x0, x1 = max(0, min(xs) - 4), min(w, max(xs) + 5)
    y0, y1 = max(0, ys[0] - 4), min(h, ys[-1] + 5)
    rgba = pymupdf.Pixmap(pymupdf.csRGB, w, h, bytes(out), True)
    crop = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, x1 - x0, y1 - y0), True)
    crop.set_origin(x0, y0)
    crop.copy(rgba, pymupdf.IRect(x0, y0, x1, y1))
    crop.set_origin(0, 0)
    return bytes(crop.tobytes("png"))


# --------------------------------------------------------------- certificados
@dataclass(frozen=True, slots=True)
class CertificateInfo:
    subject: str
    issuer: str
    not_before: dt.datetime
    not_after: dt.datetime


def _load_signer(path: Path, password: str) -> object:
    from pyhanko.sign import signers

    if not path.is_file():
        raise SignatureError(
            f"No se encuentra el certificado «{path.name}». ¿Se ha movido o borrado? "
            "Vuelve a añadirlo en Firma → Gestionar firmas."
        )
    if path.suffix.lower() not in CERT_SUFFIXES:
        raise SignatureError("El certificado debe ser un archivo .pfx o .p12.")
    # pyHanko no lanza error con una contraseña incorrecta: devuelve None y lo
    # anota en el registro. Se silencia ese registro y se comprueba el resultado.
    quiet = logging.getLogger("pyhanko")
    level = quiet.level
    quiet.setLevel(logging.CRITICAL)
    try:
        signer = signers.SimpleSigner.load_pkcs12(str(path), passphrase=password.encode("utf-8"))
    except Exception as exc:
        raise SignatureError("No se pudo leer el certificado. ¿Es un archivo .pfx/.p12 válido?") from exc
    finally:
        quiet.setLevel(level)
    if signer is None:
        raise SignatureError("Contraseña incorrecta (o el archivo no es un certificado válido).")
    return signer


def _name(subject: object) -> str:
    native = getattr(subject, "native", {}) or {}
    cn = native.get("common_name")
    if isinstance(cn, list):
        cn = cn[0]
    return str(cn or getattr(subject, "human_friendly", "Certificado"))


def read_certificate(path: Path, password: str) -> CertificateInfo:
    """Titular, emisor y validez del certificado (comprueba la contraseña)."""
    signer = _load_signer(path, password)
    cert = getattr(signer, "signing_cert")
    validity = cert["tbs_certificate"]["validity"]
    return CertificateInfo(
        subject=_name(cert.subject),
        issuer=_name(cert.issuer),
        not_before=validity["not_before"].native,
        not_after=validity["not_after"].native,
    )


# --------------------------------------------------------------------- firmar
def is_signed(doc: pymupdf.Document) -> bool:
    return any(_signature_fields(doc))


def _signature_fields(doc: pymupdf.Document) -> list[str]:
    out = []
    for i in range(doc.page_count):
        for w in doc[i].widgets():
            if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE and w.field_name:
                out.append(str(w.field_name))
    return out


@dataclass(frozen=True, slots=True)
class SignatureStatus:
    field: str
    signer: str
    intact: bool  # el documento no se ha modificado desde que se firmó
    signed_at: str


def signature_status(data: bytes) -> list[SignatureStatus]:
    """Firmas del PDF y si siguen íntegras (no se comprueba la confianza en el emisor)."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    # Solo integridad: sin raíces de confianza y sin conexión a Internet
    # (no se consultan servidores de revocación: nada sale del ordenador).
    vc = ValidationContext(trust_roots=[], allow_fetching=False)

    quiet = logging.getLogger("pyhanko")
    level = quiet.level
    quiet.setLevel(logging.CRITICAL)
    out: list[SignatureStatus] = []
    try:
        reader = PdfFileReader(io.BytesIO(data))
        for sig in reader.embedded_signatures:
            try:
                st = validate_pdf_signature(sig, signer_validation_context=vc)
                intact = bool(st.intact and st.valid)
                signer = _name(st.signing_cert.subject)
                when = st.signer_reported_dt.isoformat(" ", "minutes") if st.signer_reported_dt else ""
            except Exception:
                intact, signer, when = False, "?", ""
            out.append(SignatureStatus(sig.field_name, signer, intact, when))
    except Exception:
        log.exception("No se pudieron leer las firmas")
    finally:
        quiet.setLevel(level)
    return out


def _pdf_box(doc: pymupdf.Document, page_index: int, rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Rectángulo visual (el de pantalla) -> coordenadas PDF (origen abajo)."""
    page = doc[page_index]
    r = pymupdf.Rect(rect) * ~page.rotation_matrix * ~page.transformation_matrix
    r.normalize()
    return (r.x0, r.y0, r.x1, r.y1)


def _next_field_name(doc: pymupdf.Document) -> str:
    used = set(_signature_fields(doc))
    n = 1
    while f"Firma{n}" in used:
        n += 1
    return f"Firma{n}"


def sign_pdf(
    data: bytes,
    page_index: int,
    rect: tuple[float, float, float, float],
    cert_path: Path,
    password: str,
    out_path: Path,
    signature_image: Path | None = None,
    reason: str | None = None,
    location: str | None = None,
) -> str:
    """Firma `data` (bytes de un PDF) y escribe el resultado en `out_path`.

    `rect` es el recuadro visible en coordenadas de pantalla de la página. La
    firma se añade al final del archivo: las firmas previas siguen siendo
    válidas. Devuelve el titular del certificado.
    """
    from pyhanko.pdf_utils.font.basic import SimpleFontEngineFactory
    from pyhanko.pdf_utils.content import ImportedPdfPage
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.pdf_utils.layout import AxisAlignment, Margins, SimpleBoxLayoutRule
    from pyhanko.pdf_utils.text import TextBoxStyle
    from pyhanko.sign import fields, signers
    from pyhanko.stamp import TextStampStyle

    signer = _load_signer(cert_path, password)
    subject = _name(getattr(signer, "signing_cert").subject)
    doc = pymupdf.open("pdf", data)
    try:
        if not 0 <= page_index < doc.page_count:
            raise SignatureError("La página indicada no existe.")
        box = _pdf_box(doc, page_index, rect)
        field_name = _next_field_name(doc)
    finally:
        doc.close()

    box_w, box_h = abs(box[2] - box[0]), abs(box[3] - box[1])
    with_image = signature_image is not None
    split = int(box_w * 0.42)  # con rúbrica: rúbrica a la izquierda, texto a la derecha
    text_w = (box_w - split if with_image else box_w) - 16
    # Letra lo más grande posible (hasta 14 pt) con la que quepan las 3 líneas.
    # (pyHanko solo admite enteros en tamaños y márgenes)
    lines = ("Firmado digitalmente por", subject, "Fecha: 00/00/0000 00:00:00 CET")
    widest = max(pymupdf.get_text_length(t, "helv", 1) for t in lines)
    font_size = int(max(5, min(14, (box_h - 10) / 3.6, text_w / widest)))
    # Helvetica: fuente estándar de todo lector PDF (cubre acentos y ñ) y no
    # requiere incrustar nada ni dependencias extra de pyHanko.
    text_style = TextBoxStyle(font=SimpleFontEngineFactory("Helvetica", 0.5), font_size=font_size)
    box_w_int = round(box[2]) - round(box[0])
    tmp_dir = Path(tempfile.mkdtemp(prefix="xpdf-firma-"))
    stamp_text = "Firmado digitalmente por\n%(signer)s\nFecha: %(ts)s"
    if signature_image is not None:
        style = TextStampStyle(
            stamp_text=stamp_text,
            text_box_style=text_style,
            border_width=0,
            background=ImportedPdfPage(str(_image_as_pdf(signature_image, tmp_dir))),
            background_opacity=1.0,
            # rúbrica en la mitad izquierda, texto en la derecha
            background_layout=SimpleBoxLayoutRule(
                x_align=AxisAlignment.ALIGN_MID, y_align=AxisAlignment.ALIGN_MID,
                margins=Margins(left=4, right=box_w_int - split + 2, top=4, bottom=4),
            ),
            inner_content_layout=SimpleBoxLayoutRule(
                x_align=AxisAlignment.ALIGN_MIN, y_align=AxisAlignment.ALIGN_MID,
                margins=Margins(left=split + 4, right=4, top=4, bottom=4),
            ),
            timestamp_format="%d/%m/%Y %H:%M:%S %Z",
        )
    else:
        style = TextStampStyle(
            stamp_text=stamp_text,
            text_box_style=text_style,
            border_width=1,
            inner_content_layout=SimpleBoxLayoutRule(
                x_align=AxisAlignment.ALIGN_MID, y_align=AxisAlignment.ALIGN_MID, margins=Margins.uniform(6)
            ),
            timestamp_format="%d/%m/%Y %H:%M:%S %Z",
        )
    meta = signers.PdfSignatureMetadata(
        field_name=field_name,
        md_algorithm="sha256",
        subfilter=fields.SigSeedSubFilter.PADES,
        reason=reason or None,
        location=location or None,
        name=subject,
    )
    pdf_signer = signers.PdfSigner(
        meta,
        signer=signer,  # type: ignore[arg-type]
        stamp_style=style,
        new_field_spec=fields.SigFieldSpec(
            sig_field_name=field_name, on_page=page_index,
            box=(round(box[0]), round(box[1]), round(box[2]), round(box[3])),
        ),
    )
    out = io.BytesIO()
    try:
        writer = IncrementalPdfFileWriter(io.BytesIO(data), strict=False)
        pdf_signer.sign_pdf(writer, output=out)
    except SignatureError:
        raise
    except Exception as exc:
        log.exception("Error firmando")
        raise SignatureError("No se pudo firmar el documento.") from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    _write_atomic(out_path, out.getvalue())
    return subject


def _image_as_pdf(image: Path, folder: Path) -> Path:
    """La rúbrica como página PDF de su mismo tamaño (fondo del sello de firma).

    Así pyHanko no necesita Pillow para incluir la imagen.
    """
    pix = pymupdf.Pixmap(str(image))
    w, h = pix.width * 72 / 150, pix.height * 72 / 150  # a 150 ppp
    doc = pymupdf.open()
    page = doc.new_page(width=w, height=h)
    page.insert_image(page.rect, filename=str(image))
    out = folder / "rubrica.pdf"
    doc.save(out)
    doc.close()
    return out


def _write_atomic(target: Path, data: bytes) -> None:
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".xpdf-", suffix=".pdf", dir=target.parent)
    except OSError as exc:
        raise SignatureError(f"No se puede escribir en la carpeta «{target.parent}».") from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    except PermissionError as exc:
        raise SignatureError(f"No hay permiso para escribir «{target.name}». ¿Está abierto en otro programa?") from exc
    finally:
        tmp.unlink(missing_ok=True)
