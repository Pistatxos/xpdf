"""Motor de composición de párrafos: partición de líneas, alineación y escritura.

Por qué no `insert_htmlbox`: su motor HTML deja márgenes interiores que dependen
de la fuente, forma ligaduras («ﬁ») aunque el glifo esté vacío en una fuente
recortada, y cambia espacios por espacios duros si la fuente no tiene el glifo.
Aquí se mide cada fragmento con las métricas reales de su fuente y se escribe con
`TextWriter` en posiciones exactas, glifo a glifo: la línea base y el margen caen
exactamente donde se pide y no hay sustituciones ocultas.

Limitación: `TextWriter` no da forma a escrituras complejas (árabe, hebreo,
índicas, tailandés…). Para esas, `text.py` usa `insert_htmlbox`; ver
`needs_shaping`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import pymupdf

from xpdf.core.pdffonts import InsertedFont, NativeFont, NativeRun, write_native

Align = Literal["left", "center", "right", "justify"]
#: Fuente para componer: una cargada por PyMuPDF o la original del PDF.
AnyFont = pymupdf.Font | NativeFont

#: Rangos Unicode que necesitan motor de forma (contextual, bidireccional, etc.).
_SHAPING_RANGES = (
    (0x0590, 0x08FF),  # hebreo, árabe, siríaco, thaana, nko, samaritano...
    (0x0900, 0x0DFF),  # índicas
    (0x0E00, 0x0EFF),  # tailandés, lao
    (0x0F00, 0x0FFF),  # tibetano
    (0x1000, 0x109F),  # birmano
    (0x1780, 0x17FF),  # jemer
    (0xFB1D, 0xFDFF),  # formas de presentación hebreas/árabes
    (0xFE70, 0xFEFF),  # formas de presentación árabes B
)


def needs_shaping(text: str) -> bool:
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in _SHAPING_RANGES)


@dataclass(frozen=True, slots=True)
class Frag:
    """Fragmento de texto con una fuente concreta (sin espacios ni saltos)."""

    text: str
    font: AnyFont
    size: float
    color: int  # 0xRRGGBB
    #: Avance exacto (pt) de cada carácter, si se conoce (métricas del original,
    #: kerning incluido). Vacío: avances del programa de fuente.
    advances: tuple[float, ...] = ()

    @property
    def width(self) -> float:
        if self.advances:
            return sum(self.advances)
        return float(self.font.text_length(self.text, fontsize=self.size))


@dataclass(frozen=True, slots=True)
class Space:
    """Espacio entre palabras. `draw` es False si la fuente no tiene el glifo."""

    font: AnyFont
    size: float
    color: int
    width: float
    draw: bool


@dataclass(frozen=True, slots=True)
class Break:
    """Salto de línea forzado."""


Token = Frag | Space | Break


def space_for(font: AnyFont, size: float, color: int) -> Space:
    if font.has_glyph(32):
        return Space(font, size, color, float(font.glyph_advance(32)) * size, True)
    return Space(font, size, color, 0.25 * size, False)


@dataclass(slots=True)
class _Word:
    frags: list[Frag]

    @property
    def width(self) -> float:
        return sum(f.width for f in self.frags)


@dataclass(slots=True)
class Placed:
    x: float  # x visual del inicio
    baseline: float  # y visual de la línea base
    text: str
    font: AnyFont
    size: float
    color: int
    advances: tuple[float, ...] = ()


@dataclass(slots=True)
class Layout:
    placed: list[Placed] = field(default_factory=list)
    line_words: list[int] = field(default_factory=list)  # nº de palabras por línea
    line_extents: list[tuple[float, float, float]] = field(default_factory=list)  # (x0, x1, baseline)
    bbox: pymupdf.Rect = field(default_factory=pymupdf.Rect)

    @property
    def n_lines(self) -> int:
        return len(self.line_extents)


# --------------------------------------------------------------------- partición
@dataclass(slots=True)
class _Line:
    items: list[_Word | Space]
    hard_end: bool  # termina en salto forzado o es la última

    def natural_width(self) -> float:
        return sum(it.width for it in self.items)


def _words(tokens: Sequence[Token]) -> list[_Word | Space | Break]:
    out: list[_Word | Space | Break] = []
    for t in tokens:
        if isinstance(t, Frag):
            if out and isinstance(out[-1], _Word):
                out[-1].frags.append(t)
            else:
                out.append(_Word([t]))
        else:
            out.append(t)
    return out


def _split_long_word(word: _Word, avail: float) -> list[_Word]:
    """Parte una palabra más ancha que la línea, carácter a carácter."""
    pieces: list[_Word] = []
    cur: list[Frag] = []
    cur_w = 0.0
    for f in word.frags:
        for ch in f.text:
            g = Frag(ch, f.font, f.size, f.color)
            if cur and cur_w + g.width > avail:
                pieces.append(_Word(cur))
                cur, cur_w = [], 0.0
            if cur and cur[-1].font is g.font and cur[-1].size == g.size and cur[-1].color == g.color:
                cur[-1] = Frag(cur[-1].text + ch, g.font, g.size, g.color)
            else:
                cur.append(g)
            cur_w += g.width
    if cur:
        pieces.append(_Word(cur))
    return pieces


def break_lines(tokens: Sequence[Token], width: float, indent: float = 0.0) -> list[_Line]:
    """Partición voraz (como Word y Acrobat): llena cada línea hasta `width`."""
    lines: list[_Line] = []
    cur: list[_Word | Space] = []
    cur_w = 0.0
    pending_space: list[Space] = []

    def avail() -> float:
        return width - (indent if not lines else 0.0)

    def flush(hard: bool) -> None:
        nonlocal cur, cur_w
        lines.append(_Line(cur, hard))
        cur, cur_w = [], 0.0

    queue = _words(tokens)
    i = 0
    while i < len(queue):
        item = queue[i]
        i += 1
        if isinstance(item, Break):
            pending_space.clear()
            flush(True)
            continue
        if isinstance(item, Space):
            if cur:
                pending_space.append(item)
            continue
        sp_w = sum(s.width for s in pending_space)
        if cur and cur_w + sp_w + item.width > avail() + 1e-6:
            pending_space.clear()
            flush(False)
        if not cur and item.width > avail() + 1e-6:
            parts = _split_long_word(item, max(avail(), 1.0))
            queue[i:i] = parts[1:]
            item = parts[0]
        if cur:
            cur.extend(pending_space)
            cur_w += sum(s.width for s in pending_space)
        pending_space.clear()
        cur.append(item)
        cur_w += item.width
    if cur or not lines or (lines and lines[-1].hard_end):
        lines.append(_Line(cur, True))
    else:
        lines[-1].hard_end = True
    return lines


# --------------------------------------------------------------------- colocación
def layout(
    tokens: Sequence[Token],
    x0: float,
    x1: float,
    first_baseline: float,
    line_pitch: float,
    align: Align = "left",
    indent: float = 0.0,
    slack: float = 0.0,
) -> Layout:
    """Compone `tokens` en la caja [x0, x1] a partir de la primera línea base.

    `slack` amplía solo el ancho usado para *partir* líneas (no la caja): sirve
    para reproducir originales compuestos con kerning, cuyas líneas son algo más
    estrechas que la suma de avances de sus glifos.
    """
    width = x1 - x0
    lines = break_lines(tokens, width + slack, indent)
    out = Layout()
    bbox: pymupdf.Rect | None = None
    for n, line in enumerate(lines):
        base = first_baseline + n * line_pitch
        left = x0 + (indent if n == 0 else 0.0)
        avail = x1 - left
        natural = line.natural_width()
        gap_extra = 0.0
        if align == "justify" and not line.hard_end:
            n_spaces = sum(1 for it in line.items if isinstance(it, Space))
            if n_spaces:
                # puede ser algo negativo si la línea entró gracias a `slack`
                gap_extra = (avail - natural) / n_spaces
        if align == "right":
            x = x1 - natural
        elif align == "center":
            x = left + (avail - natural) / 2
        else:
            x = left
        start = x
        for it in line.items:
            if isinstance(it, Space):
                if it.draw:
                    out.placed.append(Placed(x, base, " ", it.font, it.size, it.color))
                x += it.width + gap_extra
                continue
            for f in it.frags:
                out.placed.append(Placed(x, base, f.text, f.font, f.size, f.color, f.advances))
                x += f.width
        out.line_extents.append((start, x, base))
        out.line_words.append(sum(1 for it in line.items if isinstance(it, _Word)))
        sizes = [f.size for it in line.items if isinstance(it, _Word) for f in it.frags] or [0.0]
        fonts = [f.font for it in line.items if isinstance(it, _Word) for f in it.frags]
        asc = max((ft.ascender for ft in fonts), default=0.8) * max(sizes)
        desc = min((ft.descender for ft in fonts), default=-0.2) * max(sizes)
        r = pymupdf.Rect(start, base - asc, max(x, start), base - desc)
        bbox = r if bbox is None else bbox | r
    out.bbox = bbox if bbox is not None else pymupdf.Rect(x0, first_baseline, x0, first_baseline)
    return out


# ----------------------------------------------------------------------- escritura
def write(page: pymupdf.Page, lay: Layout) -> None:
    """Escribe la composición en `page`, en un único flujo y en orden de lectura.

    Las fuentes originales del PDF se usan con su propio recurso; las de
    respaldo se registran en la página (`insert_font`) y se escriben por número
    de glifo. Todo va en el mismo flujo de contenido y en el orden del texto:
    si se repartiera en varios flujos, al copiar o buscar las palabras saldrían
    desordenadas. La rotación de la página la resuelve la matriz de texto.
    """
    fonts_before = {f[0] for f in page.get_fonts(full=True)}
    inserted: dict[int, InsertedFont] = {}
    runs: list[NativeRun] = []
    for p in lay.placed:
        font = p.font
        if isinstance(font, NativeFont):
            runs.append(NativeRun(p.x, p.baseline, p.text, font, p.size, p.color, p.advances))
            continue
        ins = inserted.get(id(font))
        if ins is None:
            name = "XPI" + hashlib.sha1(font.buffer).hexdigest()[:10]
            ins = inserted[id(font)] = InsertedFont(page, font, name)
        runs.append(NativeRun(p.x, p.baseline, p.text, ins, p.size, p.color, p.advances))
    if not write_native(page, runs):
        raise RuntimeError("no se pudo escribir en la página")
    _fix_tounicode(page, fonts_before)


#: Glifos que las fuentes suelen compartir entre dos caracteres. MuPDF genera la
#: tabla ToUnicode al revés (glifo -> carácter) y puede elegir el «raro», con lo
#: que al copiar saldría un espacio duro o un guion blando. Solo se corrigen
#: entradas individuales `<glifo> <carácter>` (bfchar), nunca rangos.
_BFCHAR_FIX = re.compile(rb"^(\s*<[0-9A-Fa-f]+>\s+)<00(?:[aA]0|[aA][dD])>(\s*)$", re.MULTILINE)


def _preferred(m: re.Match[bytes]) -> bytes:
    target = b"<0020>" if m.group(0).lower().rstrip().endswith(b"<00a0>") else b"<002d>"
    return m.group(1) + target + m.group(2)


def _fix_tounicode(page: pymupdf.Page, fonts_before: set[int]) -> None:
    doc = page.parent
    for f in page.get_fonts(full=True):
        if f[0] in fonts_before:
            continue
        kind, val = doc.xref_get_key(f[0], "ToUnicode")
        if kind != "xref":
            continue
        xref = int(val.split()[0])
        data = doc.xref_stream(xref)
        if data is None:
            continue
        fixed = _BFCHAR_FIX.sub(_preferred, data)
        if fixed != data:
            doc.update_stream(xref, fixed)
