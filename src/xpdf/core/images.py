"""Imágenes: listar, añadir, mover/redimensionar, reemplazar y borrar.

Cada operación actúa sobre *un dibujo concreto* de la imagen (la instrucción del
contenido que la pinta en ese sitio), no sobre el objeto de imagen: si el mismo
logo aparece en varias páginas o posiciones, solo cambia el elegido. Mover y
redimensionar envuelven ese dibujo con una transformación, así que se conserva
el orden de capas (un fondo sigue debajo del texto).

Coordenadas «visuales» (las del render, con la rotación de la página aplicada),
como en el resto de `core`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import pymupdf

from xpdf.core.contentstream import ImageDraw, add_resource, draw_order, first_text, image_draws
from xpdf.core.document import RenderedPage
from xpdf.core.errors import XpdfError

if TYPE_CHECKING:
    from xpdf.core.commands import SnapshotCommand
    from xpdf.core.document import Document

log = logging.getLogger(__name__)
T = TypeVar("T")

Rect = tuple[float, float, float, float]

#: Formatos que se pueden añadir.
SUPPORTED_SUFFIXES = (".png", ".jpg", ".jpeg")
#: Resolución máxima con la que se incrusta una imagen nueva (ppp a su tamaño en la página).
MAX_DPI = 300


class ImageError(XpdfError):
    """Operación de imagen no válida, con mensaje para el usuario."""


@dataclass(frozen=True, slots=True)
class ImageInfo:
    number: int  # identificador del dibujo en la página (orden de pintado)
    bbox: Rect  # visual
    width: int  # píxeles de la imagen
    height: int
    #: None si se puede editar; si no, el motivo (para el usuario).
    blocked_reason: str | None
    #: Opacidad aplicada por xpdf (1 = opaca).
    opacity: float = 1.0


@dataclass(slots=True)
class _Placement:
    info: ImageInfo
    transform: pymupdf.Matrix  # cuadrado unidad de la imagen -> espacio de página sin rotar
    draw: ImageDraw | None


# ------------------------------------------------------------------ consulta
def _vals(m: pymupdf.Matrix) -> tuple[float, ...]:
    return (float(m.a), float(m.b), float(m.c), float(m.d), float(m.e), float(m.f))


def _close(a: pymupdf.Matrix, b: pymupdf.Matrix, tol: float = 0.05) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(_vals(a), _vals(b)))


#: Volteo vertical del cuadrado unidad: PyMuPDF lo incluye en la matriz de cada
#: imagen (en PDF la primera fila de la imagen va arriba). Matriz de PyMuPDF =
#: F · CTM · P, con P = espacio PDF -> espacio de página sin rotar.
_FLIP = pymupdf.Matrix(1, 0, 0, -1, 0, 1)


#: Número a partir del cual se identifican las imágenes que están dentro de grupos.
_GROUP_BASE = 100_000


def _pixel_size(page: pymupdf.Page, draw: ImageDraw, names: dict[str, int]) -> tuple[int, int]:
    doc = page.parent
    if draw.name is not None and draw.name in names:
        xref = names[draw.name]
        try:
            return int(doc.xref_get_key(xref, "Width")[1]), int(doc.xref_get_key(xref, "Height")[1])
        except ValueError:
            return 0, 0
    data = (doc.xref_stream(draw.stream_xref) or b"")[draw.start : draw.end]
    w = re.search(rb"/(?:W|Width)\s+(\d+)", data)
    h = re.search(rb"/(?:H|Height)\s+(\d+)", data)
    return (int(w.group(1)) if w else 0, int(h.group(1)) if h else 0)


def _placements(page: pymupdf.Page) -> list[_Placement]:
    """Imágenes de la página.

    Se parte de los dibujos encontrados en el contenido (no de
    `get_image_info`, que deja de listar las imágenes semitransparentes). Esa
    función solo se usa para descubrir imágenes dentro de grupos, que se marcan
    como no editables.
    """
    to_mupdf = page.transformation_matrix  # P: espacio PDF -> espacio de página sin rotar
    rot = page.rotation_matrix
    doc = page.parent
    names = {str(img[7]): int(img[0]) for img in page.get_images(full=True)}
    out: list[_Placement] = []
    for idx, draw in enumerate(d for d in image_draws(page) if not d.is_form):
        t = _FLIP * draw.ctm * to_mupdf
        r = pymupdf.Rect(0, 0, 1, 1) * t * rot
        w = _wrapper(doc.xref_stream(draw.stream_xref) or b"", draw)
        pw, ph = _pixel_size(page, draw, names)
        out.append(
            _Placement(
                ImageInfo(idx, (r.x0, r.y0, r.x1, r.y1), pw, ph, None, w.alpha if w.alpha is not None else 1.0),
                t,
                draw,
            )
        )
    # Imágenes dentro de grupos (Form XObjects): visibles pero no editables.
    for info in page.get_image_info(xrefs=True):
        t = pymupdf.Matrix(info["transform"])
        if any(_close(p.transform, t) for p in out):
            continue
        r = pymupdf.Rect(info["bbox"]) * rot
        out.append(
            _Placement(
                ImageInfo(
                    _GROUP_BASE + int(info["number"]), (r.x0, r.y0, r.x1, r.y1),
                    int(info["width"]), int(info["height"]),
                    "Esta imagen forma parte de un grupo de elementos del PDF y no se puede "
                    "editar por separado.",
                ),
                t,
                None,
            )
        )
    return out


def list_images(doc: pymupdf.Document, page_index: int) -> list[ImageInfo]:
    return [p.info for p in _placements(doc[page_index])]


def image_at(doc: pymupdf.Document, page_index: int, x: float, y: float) -> ImageInfo | None:
    """La imagen bajo el punto (la de más arriba si se solapan), ignorando fondos
    que ocupan casi toda la página (si no, el fondo taparía todo lo demás)."""
    page = doc[page_index]
    area = page.rect.width * page.rect.height
    hits = []
    for info in list_images(doc, page_index):
        x0, y0, x1, y1 = info.bbox
        if x0 <= x <= x1 and y0 <= y <= y1:
            hits.append(info)
    if not hits:
        return None
    small = [h for h in hits if (h.bbox[2] - h.bbox[0]) * (h.bbox[3] - h.bbox[1]) < 0.9 * area]
    return (small or hits)[-1]


def _find(page: pymupdf.Page, number: int) -> _Placement:
    for p in _placements(page):
        if p.info.number == number:
            if p.draw is None:
                raise ImageError(p.info.blocked_reason or "No se puede editar esta imagen.")
            return p
    raise ImageError("La imagen ya no existe en la página.")


# ------------------------------------------------------------------ edición
# --------------------------------------------------------- el envoltorio
#: Envoltorio que xpdf pone alrededor de un dibujo de imagen:
#:     q [/XPAnn gs] [a b c d e f cm] <dibujo> Q
#: Se reescribe entero en cada cambio, así opacidad y transformaciones no se
#: acumulan. Un «q … cm /Im Do Q» ajustado que ya traiga el PDF (Word lo hace)
#: cuenta igual: componer su matriz con la nueva es matemáticamente correcto.
_NUM = rb"-?(?:\d+\.?\d*|\.\d+)"
_WRAP_PREFIX = re.compile(
    rb"(?:^|(?<=[\s\]\)>]))q\s+(?:/(XPA\d{1,3})\s+gs\s+)?(?:((?:" + _NUM + rb"\s+){6})cm\s+)?$"
)


@dataclass(frozen=True, slots=True)
class _Wrap:
    start: int  # bytes [start, end) del bloque completo a reescribir
    end: int
    core: bytes  # el dibujo en sí («/Im Do» o «BI … EI»)
    alpha: float | None
    matrix: pymupdf.Matrix | None  # «cm» del envoltorio, si lo hay


def _wrapper(data: bytes, draw: ImageDraw) -> _Wrap:
    core = data[draw.start : draw.end]
    window_start = max(0, draw.start - 200)
    m = _WRAP_PREFIX.search(data[window_start : draw.start])
    after = data[draw.end : draw.end + 8]
    closes = re.match(rb"\s*Q", after)
    if m and closes and (m.group(1) or m.group(2)):
        alpha = int(m.group(1)[3:]) / 100 if m.group(1) else None
        matrix = None
        if m.group(2):
            matrix = pymupdf.Matrix(*(float(x) for x in m.group(2).split()))
        return _Wrap(window_start + m.start(), draw.end + closes.end(), core, alpha, matrix)
    return _Wrap(draw.start, draw.end, core, None, None)


def _alpha_name(page: pymupdf.Page, alpha: float) -> bytes:
    pct = max(5, min(100, round(alpha * 100)))
    name = f"XPA{pct}"
    xref = page.parent.get_new_xref()
    page.parent.update_object(xref, f"<< /Type /ExtGState /ca {pct / 100:.2f} /CA {pct / 100:.2f} >>")
    if not add_resource(page, "ExtGState", name, f"{xref} 0 R"):
        raise ImageError("Esta página no admite transparencia (recursos compartidos).")
    return f"/{name}".encode()


def _wrapped(page: pymupdf.Page, core: bytes, alpha: float | None, matrix: pymupdf.Matrix | None) -> bytes:
    if (alpha is None or alpha >= 0.995) and matrix is None:
        return core
    parts = [b"q"]
    if alpha is not None and alpha < 0.995:
        parts += [_alpha_name(page, alpha), b"gs"]
    if matrix is not None:
        parts += [_fmt(matrix), b"cm"]
    return b" ".join([*parts, core, b"Q"])


def _rewrite(page: pymupdf.Page, draw: ImageDraw, new: bytes, w: _Wrap) -> None:
    doc = page.parent
    data = doc.xref_stream(draw.stream_xref) or b""
    doc.update_stream(draw.stream_xref, data[: w.start] + new + data[w.end :])


def _current(page: pymupdf.Page, number: int) -> tuple[_Placement, ImageDraw, _Wrap]:
    p = _find(page, number)
    assert p.draw is not None
    data = page.parent.xref_stream(p.draw.stream_xref) or b""
    return p, p.draw, _wrapper(data, p.draw)


# ------------------------------------------------------------------ edición
def delete_image(doc: pymupdf.Document, page_index: int, number: int) -> None:
    page = doc[page_index]
    _, draw, w = _current(page, number)
    _rewrite(page, draw, b"", w)


def _fmt(m: pymupdf.Matrix) -> bytes:
    return " ".join(f"{v:.6f}".rstrip("0").rstrip(".") or "0" for v in _vals(m)).encode()


def move_image(doc: pymupdf.Document, page_index: int, number: int, new_bbox: Rect) -> None:
    """Mueve o redimensiona la imagen para que ocupe `new_bbox` (visual)."""
    page = doc[page_index]
    p, draw, w = _current(page, number)
    unrot = ~page.rotation_matrix
    old = pymupdf.Rect(p.info.bbox) * unrot
    new = pymupdf.Rect(new_bbox) * unrot
    if new.width < 1 or new.height < 1:
        raise ImageError("La imagen es demasiado pequeña.")
    sx, sy = new.width / old.width, new.height / old.height
    # A: lleva la caja vieja a la nueva (espacio de página sin rotar).
    a = pymupdf.Matrix(sx, 0, 0, sy, new.x0 - old.x0 * sx, new.y0 - old.y0 * sy)
    # Queremos T' = T·A, con T = F·C·P. El «cm» extra se aplica antes del CTM
    # actual (C' = M·C), así que M = C·P·A·P⁻¹·C⁻¹, que con C = F·T·P⁻¹ queda
    # M = F·T·A·T⁻¹·F (sin necesidad de conocer C).
    m = _FLIP * p.transform * a * ~p.transform * _FLIP
    if w.matrix is not None:
        m = m * w.matrix  # se compone con la matriz que ya tenía el envoltorio
    _rewrite(page, draw, _wrapped(page, w.core, w.alpha, m), w)


def set_opacity(doc: pymupdf.Document, page_index: int, number: int, alpha: float) -> None:
    """Opacidad de la imagen, de 0,05 a 1 (1 = opaca)."""
    page = doc[page_index]
    _, draw, w = _current(page, number)
    _rewrite(page, draw, _wrapped(page, w.core, max(0.05, min(1.0, alpha)), w.matrix), w)


def bring_to_front(doc: pymupdf.Document, page_index: int, number: int) -> None:
    """Dibuja la imagen la última: queda por encima de todo."""
    page = doc[page_index]
    _, draw, w = _current(page, number)
    block = _wrapped(page, w.core, w.alpha, draw.ctm)  # CTM absoluta: no depende de dónde esté
    _rewrite(page, draw, b"", w)
    page.wrap_contents()  # que el contenido previo no deje su estado gráfico
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, b"\n" + block + b"\n")
    contents = [*page.get_contents(), xref]
    doc.xref_set_key(page.xref, "Contents", "[" + " ".join(f"{x} 0 R" for x in contents) + "]")


def send_behind_text(doc: pymupdf.Document, page_index: int, number: int) -> bool:
    """Dibuja la imagen justo antes del primer texto: queda detrás del texto pero
    delante de fondos y dibujos anteriores. False si ya estaba detrás."""
    page = doc[page_index]
    _, draw, w = _current(page, number)
    pos = first_text(page)
    if pos is None or draw_order(page, draw) < pos.order:
        return False
    # Matriz relativa a la CTM vigente en el punto de inserción.
    block = _wrapped(page, w.core, w.alpha, draw.ctm * ~pos.ctm) + b"\n"
    if pos.stream_xref == draw.stream_xref:
        data = doc.xref_stream(draw.stream_xref) or b""
        assert pos.offset < w.start  # el texto va antes que la imagen en este flujo
        new = data[: pos.offset] + block + data[pos.offset : w.start] + data[w.end :]
        doc.update_stream(draw.stream_xref, new)
    else:
        _rewrite(page, draw, b"", w)
        data = doc.xref_stream(pos.stream_xref) or b""
        doc.update_stream(pos.stream_xref, data[: pos.offset] + block + data[pos.offset :])
    return True


def _image_bytes(path: Path, target: pymupdf.Rect) -> bytes:
    """Bytes a incrustar: el archivo tal cual, o reducido si sobra resolución."""
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ImageError("Formato no admitido: usa una imagen PNG o JPG.")
    try:
        raw = path.read_bytes()
        pix = pymupdf.Pixmap(raw)
    except Exception as exc:
        raise ImageError(f"No se pudo leer la imagen «{path.name}».") from exc
    need = max(target.width, target.height) / 72 * MAX_DPI
    longest = max(pix.width, pix.height)
    if longest <= 1.5 * need:
        return raw
    f = need / longest
    small = pymupdf.Pixmap(pix, max(1, round(pix.width * f)), max(1, round(pix.height * f)), None)
    log.info("Imagen %s reducida de %dx%d a %dx%d", path.name, pix.width, pix.height, small.width, small.height)
    if small.alpha:
        return bytes(small.tobytes("png"))
    if small.colorspace and small.colorspace.n not in (1, 3):
        small = pymupdf.Pixmap(pymupdf.csRGB, small)
    return bytes(small.tobytes("jpeg", jpg_quality=88))


def fit_rect(img_w: float, img_h: float, box: Rect) -> Rect:
    """Rectángulo centrado en `box` con la proporción de la imagen."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    s = min(bw / img_w, bh / img_h)
    w, h = img_w * s, img_h * s
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def image_size(path: Path) -> tuple[int, int]:
    try:
        pix = pymupdf.Pixmap(path.read_bytes())
    except Exception as exc:
        raise ImageError(f"No se pudo leer la imagen «{path.name}».") from exc
    return pix.width, pix.height


def add_image(doc: pymupdf.Document, page_index: int, bbox: Rect, path: Path) -> Rect:
    """Añade la imagen ocupando `bbox` (visual, se respeta la proporción). Devuelve su caja."""
    page = doc[page_index]
    w, h = image_size(path)
    box = fit_rect(w, h, bbox)
    raw_rect = pymupdf.Rect(box) * ~page.rotation_matrix
    data = _image_bytes(path, raw_rect)
    # `rotate` endereza la imagen en páginas giradas (se ve derecha en pantalla).
    page.insert_image(raw_rect, stream=data, keep_proportion=False, rotate=(-page.rotation) % 360)
    return box


def replace_image(doc: pymupdf.Document, page_index: int, number: int, path: Path) -> Rect:
    """Sustituye la imagen por otra en el mismo sitio (encajada con su proporción)."""
    page = doc[page_index]
    p = _find(page, number)
    bbox = p.info.bbox
    delete_image(doc, page_index, number)
    return add_image(doc, page_index, bbox, path)


# --------------------------------------------------------------- previsualizar
def render_without_image(doc: pymupdf.Document, page_index: int, number: int, scale: float) -> RenderedPage:
    """Render de la página sin esa imagen (fondo para arrastrarla en la interfaz)."""
    tmp = pymupdf.open()
    try:
        tmp.insert_pdf(doc, from_page=page_index, to_page=page_index)
        try:
            delete_image(tmp, 0, number)
        except ImageError:
            pass
        pix = tmp[0].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=True)
        return RenderedPage(pix.width, pix.height, pix.stride, bytes(pix.samples))
    finally:
        tmp.close()


def render_image_only(doc: pymupdf.Document, page_index: int, number: int, scale: float) -> RenderedPage:
    """La zona de la imagen tal como se ve (para arrastrar una vista previa)."""
    page = doc[page_index]
    for info in list_images(doc, page_index):
        if info.number == number:
            clip = pymupdf.Rect(info.bbox) * ~page.rotation_matrix
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
            return RenderedPage(pix.width, pix.height, pix.stride, bytes(pix.samples))
    raise ImageError("La imagen ya no existe en la página.")


# ------------------------------------------------------------------ comandos
def _cmd(doc: "Document", label: str, action: "Callable[[], T]", page: int) -> "SnapshotCommand[T]":
    from xpdf.core.commands import SnapshotCommand

    return SnapshotCommand(doc, label, action, (page,))


def delete_command(doc: "Document", page: int, number: int) -> "SnapshotCommand[None]":
    return _cmd(doc, "borrar imagen", lambda: delete_image(doc.raw, page, number), page)


def move_command(doc: "Document", page: int, number: int, bbox: Rect) -> "SnapshotCommand[None]":
    return _cmd(doc, "mover imagen", lambda: move_image(doc.raw, page, number, bbox), page)


def replace_command(doc: "Document", page: int, number: int, path: Path) -> "SnapshotCommand[Rect]":
    return _cmd(doc, "reemplazar imagen", lambda: replace_image(doc.raw, page, number, path), page)


def add_command(doc: "Document", page: int, bbox: Rect, path: Path) -> "SnapshotCommand[Rect]":
    return _cmd(doc, "añadir imagen", lambda: add_image(doc.raw, page, bbox, path), page)


def opacity_command(doc: "Document", page: int, number: int, alpha: float) -> "SnapshotCommand[None]":
    return _cmd(doc, "opacidad de la imagen", lambda: set_opacity(doc.raw, page, number, alpha), page)


def front_command(doc: "Document", page: int, number: int) -> "SnapshotCommand[None]":
    return _cmd(doc, "traer imagen al frente", lambda: bring_to_front(doc.raw, page, number), page)


def behind_text_command(doc: "Document", page: int, number: int) -> "SnapshotCommand[bool]":
    return _cmd(doc, "enviar imagen detrás del texto", lambda: send_behind_text(doc.raw, page, number), page)


def images_on(doc: "Document", page: int) -> list[ImageInfo]:
    return list_images(doc.raw, page)


def image_under(doc: "Document", page: int, x: float, y: float) -> ImageInfo | None:
    return image_at(doc.raw, page, x, y)


def preview_without(doc: "Document", page: int, number: int, scale: float) -> RenderedPage:
    return render_without_image(doc.raw, page, number, scale)


def preview_image(doc: "Document", page: int, number: int, scale: float) -> RenderedPage:
    return render_image_only(doc.raw, page, number, scale)
