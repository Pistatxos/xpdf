"""Fachada de edición de texto para la interfaz.

La UI trabaja con tipos simples (`ParagraphInfo`, `StyleInfo`, `Span`) y recibe
comandos listos para el historial. Nada de aquí expone objetos de PyMuPDF.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Literal

import pymupdf

from xpdf.core.commands import SnapshotCommand
from xpdf.core.document import Document, RenderedPage
from xpdf.core.fonts import FontRequest, FontResolver, classify, clean_font_name
from xpdf.core.text import (
    Align,
    EditResult,
    Paragraph,
    Run,
    TextStyle,
    add_text,
    detect_paragraphs,
    redact_lines,
    replace_paragraph,
)

log = logging.getLogger(__name__)

FontClass = Literal["serif", "sans", "mono"]

#: Fuentes base para el texto nuevo; el resolvedor encuentra la equivalente del
#: sistema (Helvetica/Arial, Times, Courier) o la incluida (Liberation).
_NEW_TEXT_FONTS: dict[FontClass, str] = {
    "sans": "Helvetica",
    "serif": "Times-Roman",
    "mono": "Courier",
}
_F_ITALIC, _F_SERIF, _F_MONO, _F_BOLD = 2, 4, 8, 16

#: Caracteres que se comprueban para la vista previa del editor: latín
#: extendido, puntuación general y símbolos habituales.
_PREVIEW_CHARS = "".join(
    chr(c)
    for c in [*range(0x21, 0x7F), *range(0xA1, 0x250), *range(0x2010, 0x2040), 0x20AC, 0x2122]
)


@dataclass(frozen=True, slots=True)
class StyleInfo:
    """Estilo de un tramo, para que la UI muestre el editor con el mismo aspecto."""

    index: int
    font_name: str  # nombre limpio, para mostrar
    font_class: FontClass
    size: float
    color: int  # 0xRRGGBB
    bold: bool
    italic: bool
    #: Archivo de la fuente con la que se escribirá (para cargarla en el editor), o None.
    font_data: bytes | None
    #: Si `font_data` es un recorte incrustado: caracteres que dibuja de verdad.
    #: None = fuente completa. Los demás se muestran con `fallback_data`.
    covered: frozenset[str] | None = None
    fallback_data: bytes | None = None


@dataclass(frozen=True, slots=True)
class ParagraphInfo:
    id: int
    page: int
    bbox: tuple[float, float, float, float]  # coordenadas visuales (puntos)
    box: tuple[float, float]  # ancho disponible (x0, x1) para recomponer
    line_rects: tuple[tuple[float, float, float, float], ...]
    align: Align
    line_pitch: float
    indent: float
    first_baseline: float
    spans: tuple[tuple[str, int], ...]  # (texto, índice de estilo)
    styles: tuple[StyleInfo, ...]
    blocked_reason: str | None

    @property
    def text(self) -> str:
        return "".join(t for t, _ in self.spans)


@dataclass(frozen=True, slots=True)
class Span:
    """Tramo editado devuelto por la UI.

    `style` es el índice de un `StyleInfo` del párrafo; `bold`/`italic`/`size`/
    `color` a None conservan el del estilo.
    """

    text: str
    style: int
    bold: bool | None = None
    italic: bool | None = None
    size: float | None = None
    color: int | None = None


@dataclass(frozen=True, slots=True)
class NewTextStyle:
    font_class: FontClass = "sans"
    size: float = 12.0
    color: int = 0x000000
    bold: bool = False
    italic: bool = False


@dataclass(frozen=True, slots=True)
class TextOutcome:
    bbox: tuple[float, float, float, float]
    #: Avisos de sustitución de fuentes, para la barra de estado.
    substitutions: tuple[str, ...]
    #: El párrafo creció y se monta sobre texto de debajo.
    overlaps: bool = False

    def message(self) -> str:
        parts = list(self.substitutions)
        if self.overlaps:
            parts.append("el texto se superpone con el contenido de debajo; puedes acortarlo o deshacer")
        return " · ".join(parts)


class TextEditing:
    """Edición de texto de un documento, con caché de párrafos por página."""

    def __init__(self, doc: Document) -> None:
        self.doc = doc
        self._cache: dict[int, tuple[int, list[Paragraph]]] = {}

    # ----------------------------------------------------------------- lectura
    def _paragraphs(self, page: int) -> list[Paragraph]:
        hit = self._cache.get(page)
        if hit is not None and hit[0] == self.doc.version:
            return hit[1]
        paras = detect_paragraphs(self.doc.raw, page)
        self._cache[page] = (self.doc.version, paras)
        return paras

    def paragraphs(self, page: int) -> list[ParagraphInfo]:
        resolver = FontResolver(self.doc.raw)
        return [self._info(p, resolver) for p in self._paragraphs(page)]

    def hit(self, page: int, x: float, y: float) -> tuple[int, tuple[float, float, float, float], str | None] | None:
        """Consulta ligera para el resaltado: (id, caja, motivo de bloqueo) o None."""
        for p in self._paragraphs(page):
            x0, y0, x1, y1 = p.bbox
            if x0 - 2 <= x <= x1 + 2 and y0 - 2 <= y <= y1 + 2:
                return p.id, p.bbox, p.blocked_reason
        return None

    def paragraph(self, page: int, para_id: int) -> ParagraphInfo:
        return self._info(self._find(page, para_id), FontResolver(self.doc.raw))

    def paragraph_at(self, page: int, x: float, y: float) -> ParagraphInfo | None:
        h = self.hit(page, x, y)
        return self.paragraph(page, h[0]) if h is not None else None

    def style_preview(self, style: NewTextStyle) -> StyleInfo:
        """`StyleInfo` para mostrar en el editor el texto nuevo con este estilo."""
        flags = _flags_for(style)
        name = _NEW_TEXT_FONTS[style.font_class]
        try:
            data: bytes | None = FontResolver(self.doc.raw).resolve(
                FontRequest(name, flags, 0), "Aa"
            ).buffer
        except Exception:
            log.exception("No se pudo resolver la fuente para el texto nuevo")
            data = None
        return StyleInfo(0, name, style.font_class, style.size, style.color, style.bold, style.italic, data)

    def _info(self, p: Paragraph, resolver: FontResolver) -> ParagraphInfo:
        styles: list[TextStyle] = []
        spans: list[tuple[str, int]] = []
        for r in p.runs:
            if r.style not in styles:
                styles.append(r.style)
            spans.append((r.text, styles.index(r.style)))
        infos = []
        for i, st in enumerate(styles):
            fs = classify(st.font, st.flags)
            data: bytes | None = None
            covered: frozenset[str] | None = None
            fallback: bytes | None = None
            bold, italic = fs.bold, fs.italic
            try:
                req = FontRequest(st.font, st.flags, p.page_index)
                rf = resolver.resolve(req, p.text)
                data = rf.buffer
                bold, italic = rf.bold, rf.italic  # estilo real, no el de las banderas
                if rf.source == "embedded":
                    # Los recortes tienen glifos vacíos para lo que no se usaba:
                    # el editor debe saber qué puede dibujar y con qué suplir el resto.
                    missing = resolver.missing_glyphs(data, _PREVIEW_CHARS)
                    covered = frozenset(_PREVIEW_CHARS) - frozenset(missing)
                    if missing:
                        fallback = resolver.resolve(req, "".join(sorted(missing))).buffer
            except Exception:  # la UI puede vivir sin la fuente exacta
                log.exception("No se pudo resolver la fuente %s", st.font)
            infos.append(
                StyleInfo(
                    i, clean_font_name(st.font), fs.cls, st.size, st.color, bold, italic,
                    data, covered, fallback,
                )
            )
        return ParagraphInfo(
            id=p.id,
            page=p.page_index,
            bbox=p.bbox,
            box=p.box,
            line_rects=tuple(p.line_rects),
            align=p.align,
            line_pitch=p.line_pitch,
            indent=p.indent,
            first_baseline=p.first_baseline,
            spans=tuple(spans),
            styles=tuple(infos),
            blocked_reason=p.blocked_reason,
        )

    def render_without(self, page: int, para_id: int, scale: float) -> RenderedPage:
        """Renderiza la página sin el texto del párrafo (fondo intacto).

        Sirve de fondo para el editor superpuesto: el usuario ve el texto que
        escribe en su sitio, sin el original debajo.
        """
        para = self._find(page, para_id)
        tmp = pymupdf.open()
        try:
            tmp.insert_pdf(self.doc.raw, from_page=page, to_page=page)
            redact_lines(tmp[0], para.lines)  # la misma redacción que al editar
            tp = tmp[0]
            pix = tp.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=True)
            return RenderedPage(pix.width, pix.height, pix.stride, bytes(pix.samples))
        finally:
            tmp.close()

    def _find(self, page: int, para_id: int) -> Paragraph:
        for p in self._paragraphs(page):
            if p.id == para_id:
                return p
        raise LookupError(f"Párrafo {para_id} no encontrado en la página {page}")

    # ---------------------------------------------------------------- escritura
    def edit_command(
        self,
        page: int,
        para_id: int,
        spans: list[Span],
        align: Align | None = None,
        box: tuple[float, float] | None = None,
    ) -> SnapshotCommand[TextOutcome]:
        """Comando que sustituye el párrafo por `spans` (para `History.push`)."""
        para = self._find(page, para_id)
        styles: list[TextStyle] = []
        for r in para.runs:
            if r.style not in styles:
                styles.append(r.style)
        runs = [Run(s.text, _apply(styles[s.style], s)) for s in spans if s.text]

        def action() -> TextOutcome:
            # Se vuelve a detectar sobre el estado actual (por si se rehízo antes).
            current = self._find(page, para_id)
            res = replace_paragraph(
                self.doc.raw, current, runs, FontResolver(self.doc.raw), align, box
            )
            return _outcome(res)

        return SnapshotCommand(self.doc, "editar texto", action, (page,))

    def add_command(
        self,
        page: int,
        x: float,
        y: float,
        text: str,
        style: NewTextStyle,
        width: float | None = None,
    ) -> SnapshotCommand[TextOutcome]:
        """Comando que añade `text` con la esquina superior izquierda en (x, y)."""
        st = TextStyle(_NEW_TEXT_FONTS[style.font_class], _flags_for(style), style.size, style.color)

        def action() -> TextOutcome:
            res = add_text(
                self.doc.raw, page, x, y, [Run(text, st)], FontResolver(self.doc.raw), width
            )
            # En texto nuevo no hay «original» que sustituir: solo interesa avisar
            # de caracteres que la fuente elegida no tiene.
            res = replace(res, substitutions=tuple(s for s in res.substitutions if s[2]))
            return _outcome(res)

        return SnapshotCommand(self.doc, "añadir texto", action, (page,))


def _flags_for(style: NewTextStyle) -> int:
    flags = {"sans": 0, "serif": _F_SERIF, "mono": _F_MONO}[style.font_class]
    return flags | (_F_BOLD if style.bold else 0) | (_F_ITALIC if style.italic else 0)


def _apply(st: TextStyle, span: Span) -> TextStyle:
    flags = st.flags
    if span.bold is not None:
        flags = (flags | _F_BOLD) if span.bold else (flags & ~_F_BOLD)
    if span.italic is not None:
        flags = (flags | _F_ITALIC) if span.italic else (flags & ~_F_ITALIC)
    font = st.font
    fs = classify(font, st.flags)
    want_bold = fs.bold if span.bold is None else span.bold
    want_italic = fs.italic if span.italic is None else span.italic
    if (want_bold, want_italic) != (fs.bold, fs.italic):
        # Se pide otra cara de la misma familia («Calibri» -> «Calibri-Bold»): si
        # ya está incrustada en la página se reutiliza; si no, sistema o incluida.
        font = _face_name(font, want_bold, want_italic)
    return replace(
        st,
        font=font,
        flags=flags,
        size=span.size if span.size is not None else st.size,
        color=span.color if span.color is not None else st.color,
    )


def _face_name(font: str, bold: bool, italic: bool) -> str:
    family = re.split(r"[-,]", clean_font_name(font), maxsplit=1)[0]
    family = re.sub(r"(?i)(bold|italic|oblique|regular|roman)$", "", family) or family
    suffix = {(False, False): "", (True, False): "-Bold", (False, True): "-Italic", (True, True): "-BoldItalic"}
    return family + suffix[(bold, italic)]


def _outcome(res: EditResult) -> TextOutcome:
    subs = []
    for orig, used, chars in res.substitutions:
        if chars:
            shown = " ".join(chars[:12]) + (" …" if len(chars) > 12 else "")
            subs.append(f"«{orig}» no tiene {shown}: se usó {used} para esos caracteres")
        else:
            subs.append(f"Fuente «{orig}» sustituida por {used}")
    return TextOutcome(res.bbox, tuple(subs), res.overlaps)
