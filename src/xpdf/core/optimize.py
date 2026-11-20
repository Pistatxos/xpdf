"""Reducir el tamaño de un PDF guardando una copia más ligera.

Flujo pensado para la UI:

1. `snapshot(doc)` en el hilo principal: captura el estado actual del documento
   como bytes inmutables (sin tocar el documento abierto).
2. `optimize_snapshot(snap, destino, nivel, progress)` en cualquier hilo: abre su
   propia copia PyMuPDF, la optimiza y escribe el resultado de forma atómica.

`optimize(doc, ...)` combina ambos pasos para uso síncrono.

Las imágenes se recomprimen con código propio (no con `Document.rewrite_images`):
en MuPDF 1.28 el reescritor de imágenes reescribe los flujos de contenido y en
PDFs reales (sombreados dentro de Form XObjects compartidos) deja recursos a
`null` y páginas en blanco. Aquí solo se sustituye el flujo de cada imagen en su
propio xref, sin tocar el contenido de las páginas.
"""

from __future__ import annotations

import logging
import math
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pymupdf

from xpdf.core.document import SAVE_OPTIONS, Document
from xpdf.core.errors import SaveError, XpdfError

log = logging.getLogger(__name__)

Progress = Callable[[float], None]

#: Opciones de guardado sin pérdida (las mismas que usa `Document.save_as`).
LOSSLESS_SAVE_OPTIONS = SAVE_OPTIONS


class Level(Enum):
    """Nivel de reducción."""

    LOSSLESS = "lossless"
    BALANCED = "balanced"
    SMALL = "small"


@dataclass(frozen=True, slots=True)
class ImagePolicy:
    """Cuándo y cómo recomprimir imágenes.

    Las imágenes cuya resolución efectiva (en la página) supera `threshold_dpi`
    se reducen a `target_dpi`; nunca se amplían. Todas las candidatas se
    codifican en JPEG con `quality` y solo se sustituyen si el resultado es
    claramente menor.
    """

    threshold_dpi: float
    target_dpi: float
    quality: int


POLICIES: dict[Level, ImagePolicy | None] = {
    Level.LOSSLESS: None,
    # Umbral 1,5× el objetivo (como Ghostscript): no merece la pena reescalar
    # una imagen de 160 ppp a 150.
    Level.BALANCED: ImagePolicy(threshold_dpi=225, target_dpi=150, quality=80),
    Level.SMALL: ImagePolicy(threshold_dpi=144, target_dpi=96, quality=60),
}

#: Imágenes con flujo menor que esto no se tocan: el ahorro sería despreciable.
MIN_IMAGE_BYTES = 16 * 1024
#: Ratio mínimo de mejora exigido al recomprimir sin reescalar.
#: Un JPEG que se vuelve a codificar pierde calidad: solo compensa si ahorra ≥10 %.
JPEG_REENCODE_MAX_RATIO = 0.9
#: Una imagen sin pérdida (PNG, diagrama, captura) solo pasa a JPEG si se queda
#: en la mitad o menos; así no se estropean gráficos de líneas para ganar poco.
LOSSLESS_TO_JPEG_MAX_RATIO = 0.5

_SKIP_FILTERS = {"/JBIG2Decode", "/CCITTFaxDecode"}
_LOSSY_FILTERS = {"/DCTDecode", "/JPXDecode"}
_UNSUPPORTED_CS = ("/Separation", "/DeviceN", "/Lab", "/Pattern", "/NChannel")
_DEVICE_CS = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}


class OptimizeError(XpdfError):
    """No se pudo crear la copia reducida."""


class OptimizeCancelled(XpdfError):
    """El usuario canceló la operación. El callback de progreso puede lanzarla."""

    def __init__(self, message: str = "Operación cancelada.") -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Estado inmutable de un documento, seguro de pasar a otro hilo."""

    data: bytes
    name: str
    source_path: Path | None
    has_signatures: bool
    has_forms: bool

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class OptimizeResult:
    original_bytes: int
    new_bytes: int
    #: False si no se pudo reducir y se escribió el original tal cual.
    reduced: bool
    images_recompressed: int
    fonts_subset: bool

    @property
    def saved_bytes(self) -> int:
        return self.original_bytes - self.new_bytes

    @property
    def ratio(self) -> float:
        """new/original (1.0 = igual)."""
        return self.new_bytes / self.original_bytes if self.original_bytes else 1.0


# ------------------------------------------------------------------- API pública
def snapshot(doc: Document) -> Snapshot:
    """Captura el estado actual de `doc`. Llamar desde el hilo que usa `doc`.

    Si el documento no tiene cambios sin guardar se usan los bytes del archivo
    en disco (el original exacto); si no, una serialización fiel del estado en
    memoria. El documento abierto no se modifica.
    """
    raw = doc.raw
    path = doc.path
    data: bytes | None = None
    if path is not None and not raw.is_dirty:
        try:
            data = path.read_bytes()
        except OSError:
            log.warning("No se pudo releer %s; se serializa desde memoria", path)
    if data is None:
        try:
            data = raw.tobytes()
        except Exception as exc:
            log.exception("Error serializando documento")
            raise OptimizeError("No se pudo leer el documento actual.") from exc
    try:
        has_forms = bool(raw.is_form_pdf)
        has_signatures = has_forms and int(raw.get_sigflags()) > 0
    except Exception:
        has_forms = has_signatures = False
    name = path.stem if path is not None else "documento"
    return Snapshot(data, name, path, has_signatures, has_forms)


def optimize(
    doc: Document, target: str | os.PathLike[str], level: Level, progress: Progress | None = None
) -> OptimizeResult:
    """Escribe en `target` una copia reducida de `doc` (que no se modifica)."""
    return optimize_snapshot(snapshot(doc), target, level, progress)


def optimize_snapshot(
    snap: Snapshot,
    target: str | os.PathLike[str],
    level: Level,
    progress: Progress | None = None,
) -> OptimizeResult:
    """Optimiza `snap` y lo escribe en `target` de forma atómica.

    Trabaja sobre un documento PyMuPDF propio, así que puede ejecutarse en otro
    hilo mientras ese documento no se comparta. Nunca escribe un archivo mayor
    que el original: si no hay ahorro, guarda los bytes originales.
    """
    report = _monotonic(progress)
    dest = Path(target)
    if snap.source_path is not None and _same_file(dest, snap.source_path):
        raise OptimizeError("Elige un nombre distinto al del documento original.")

    report(0.0)
    try:
        work = pymupdf.open("pdf", snap.data)
    except Exception as exc:
        log.exception("Error abriendo copia de trabajo")
        raise OptimizeError("No se pudo preparar la copia del documento.") from exc
    try:
        images = 0
        policy = POLICIES[level]
        if policy is not None:
            images = _recompress_images(work, policy, lambda f: report(0.02 + 0.68 * f))
        report(0.7)
        fonts = False
        if not snap.has_forms:
            # Con formularios no se subconjuntan fuentes: los campos podrían
            # necesitar glifos que no aparecen en el texto de las páginas.
            fonts = _subset_fonts(work)
        report(0.8)
        _drop_thumbnails(work)
        new_size = _write_atomic(work, snap.data, dest)
    except XpdfError:
        raise
    except Exception as exc:
        log.exception("Error optimizando %s", snap.name)
        raise OptimizeError(f"No se pudo crear «{dest.name}».") from exc
    finally:
        work.close()
    report(1.0)
    reduced = new_size < snap.size
    log.info("Reducido %s: %d → %d bytes (%s)", snap.name, snap.size, new_size, level.value)
    return OptimizeResult(snap.size, new_size, reduced, images if reduced else 0, fonts and reduced)


# ----------------------------------------------------------------- formateo
def format_size(n: int) -> str:
    """Tamaño legible con coma decimal: «12,4 MB», «830 KB», «512 B»."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            text = f"{value:.1f}" if value < 100 else f"{value:.0f}"
            return f"{text.replace('.', ',')} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def format_result(result: OptimizeResult) -> str:
    """«12,4 MB → 3,1 MB (−75 %)»."""
    pct = round((1 - result.ratio) * 100)
    sign = "−" if pct > 0 else ""
    return (
        f"{format_size(result.original_bytes)} → {format_size(result.new_bytes)} "
        f"({sign}{abs(pct)} %)"
    )


# ------------------------------------------------------------------ interno
def _monotonic(progress: Progress | None) -> Progress:
    """Envuelve el callback para que reciba valores crecientes en [0, 1]."""
    last = 0.0

    def report(fraction: float) -> None:
        nonlocal last
        last = min(1.0, max(last, fraction))
        if progress is not None:
            progress(last)

    return report


def _same_file(a: Path, b: Path) -> bool:
    try:
        if a.exists() and b.exists():
            return a.samefile(b)
    except OSError:
        pass
    return a.resolve() == b.resolve()


def _image_min_dpi(doc: pymupdf.Document, progress: Progress) -> dict[int, float]:
    """Resolución efectiva mínima de cada imagen (xref) entre todos sus usos."""
    result: dict[int, float] = {}
    n = doc.page_count
    for i in range(n):
        for info in doc[i].get_image_info(xrefs=True):
            xref = int(info.get("xref", 0))
            if xref <= 0:
                continue
            a, b, c, d = info["transform"][:4]
            shown_w, shown_h = math.hypot(a, b), math.hypot(c, d)
            if shown_w <= 0 or shown_h <= 0:
                continue
            dpi = min(info["width"] * 72 / shown_w, info["height"] * 72 / shown_h)
            result[xref] = min(result.get(xref, math.inf), dpi)
        progress((i + 1) / n)
    return result


def _mask_xrefs(doc: pymupdf.Document) -> set[int]:
    """xrefs usados como máscara (SMask/Mask) por otras imágenes."""
    masks: set[int] = set()
    for xref in range(1, doc.xref_length()):
        for key in ("SMask", "Mask"):
            kind, val = doc.xref_get_key(xref, key)
            if kind == "xref":
                masks.add(int(val.split()[0]))
    return masks


def _resolved_colorspace(doc: pymupdf.Document, xref: int) -> tuple[str, str]:
    """(valor tal cual para reescribir, texto resuelto para inspeccionar)."""
    kind, val = doc.xref_get_key(xref, "ColorSpace")
    if kind == "xref":
        return val, doc.xref_object(int(val.split()[0]), compressed=True)
    return val, val


def _icc_components(doc: pymupdf.Document, cs_text: str) -> int | None:
    """Número de componentes si el espacio es `[/ICCBased n 0 R]`, si no None."""
    match = re.fullmatch(r"\s*\[\s*/ICCBased\s+(\d+)\s+0\s+R\s*\]\s*", cs_text)
    if match is None:
        return None
    kind, val = doc.xref_get_key(int(match.group(1)), "N")
    return int(val) if kind == "int" else None


def _recompress_images(doc: pymupdf.Document, policy: ImagePolicy, progress: Progress) -> int:
    dpis = _image_min_dpi(doc, lambda f: progress(0.2 * f))
    masks = _mask_xrefs(doc)
    candidates = [x for x in sorted(dpis) if x not in masks]
    done = 0
    for k, xref in enumerate(candidates):
        try:
            if _recompress_one(doc, xref, dpis[xref], policy):
                done += 1
        except Exception:
            # Una imagen rara no debe impedir reducir el resto.
            log.warning("No se pudo recomprimir la imagen xref %d", xref, exc_info=True)
        progress(0.2 + 0.8 * (k + 1) / max(1, len(candidates)))
    return done


def _recompress_one(doc: pymupdf.Document, xref: int, dpi: float, policy: ImagePolicy) -> bool:
    get = doc.xref_get_key
    if get(xref, "Subtype")[1] != "/Image" or get(xref, "ImageMask")[1] == "true":
        return False
    if get(xref, "BitsPerComponent")[1] == "1":
        return False  # bitonal: CCITT/JBIG2/Flate ya son eficientes y sin pérdida
    if get(xref, "Mask")[0] == "array" or get(xref, "SMaskInData")[1] not in ("null", "0"):
        return False  # máscara por clave de color o alfa embebido: JPEG la rompería
    filt = get(xref, "Filter")[1]
    if any(f in filt for f in _SKIP_FILTERS):
        return False
    raw_len = len(doc.xref_stream_raw(xref) or b"")
    if raw_len < MIN_IMAGE_BYTES:
        return False
    cs_value, cs_text = _resolved_colorspace(doc, xref)
    if any(s in cs_text for s in _UNSUPPORTED_CS):
        return False

    pix = pymupdf.Pixmap(doc, xref)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.colorspace is None or pix.n not in _DEVICE_CS:
        return False
    n = pix.n

    downsample = dpi > policy.threshold_dpi
    if downsample:
        scale = policy.target_dpi / dpi
        w = max(1, round(pix.width * scale))
        h = max(1, round(pix.height * scale))
        pix = pymupdf.Pixmap(pix, w, h, None)
    data = pix.tobytes("jpg", jpg_quality=policy.quality)

    if downsample:
        limit = 1.0
    elif any(f in filt for f in _LOSSY_FILTERS):
        limit = JPEG_REENCODE_MAX_RATIO
    else:
        limit = LOSSLESS_TO_JPEG_MAX_RATIO
    if len(data) >= raw_len * limit:
        return False

    keep_icc = _icc_components(doc, cs_text) == n
    doc.update_stream(xref, data, compress=0)
    doc.xref_set_key(xref, "Filter", "/DCTDecode")
    doc.xref_set_key(xref, "DecodeParms", "null")
    doc.xref_set_key(xref, "Decode", "null")  # ya aplicado al decodificar
    doc.xref_set_key(xref, "Width", str(pix.width))
    doc.xref_set_key(xref, "Height", str(pix.height))
    doc.xref_set_key(xref, "BitsPerComponent", "8")
    doc.xref_set_key(xref, "ColorSpace", cs_value if keep_icc else _DEVICE_CS[n])
    return True


def _subset_fonts(doc: pymupdf.Document) -> bool:
    try:
        doc.subset_fonts()
    except Exception:
        log.warning("No se pudieron subconjuntar las fuentes", exc_info=True)
        return False
    return True


def _drop_thumbnails(doc: pymupdf.Document) -> None:
    """Las miniaturas incrustadas (/Thumb) no las usa casi ningún lector."""
    for i in range(doc.page_count):
        page = doc[i]
        if doc.xref_get_key(page.xref, "Thumb")[0] != "null":
            doc.xref_set_key(page.xref, "Thumb", "null")


def _write_atomic(work: pymupdf.Document, original: bytes, dest: Path) -> int:
    """Guarda `work` en `dest` (o `original` si no es más pequeño). Devuelve bytes."""
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".xpdf-", suffix=".pdf", dir=dest.parent)
    except PermissionError as exc:
        raise SaveError(f"No hay permiso para escribir en la carpeta «{dest.parent}».") from exc
    except OSError as exc:
        log.exception("Error creando temporal en %s", dest.parent)
        raise SaveError(f"No se pudo guardar «{dest.name}».") from exc
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        work.save(tmp, **LOSSLESS_SAVE_OPTIONS)
        size = tmp.stat().st_size
        if size >= len(original):
            tmp.write_bytes(original)
            size = len(original)
        try:
            os.replace(tmp, dest)
        except PermissionError as exc:
            raise SaveError(
                f"No hay permiso para escribir «{dest.name}». ¿Está abierto en otro programa?"
            ) from exc
    finally:
        tmp.unlink(missing_ok=True)
    return size
