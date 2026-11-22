"""Detección de párrafos y edición de texto con reflujo.

Flujo de una edición:

1. `detect_paragraphs` agrupa las líneas de `page.get_text("dict")` en párrafos
   por proximidad, tamaño y alineación, y conserva los estilos por tramos (`Run`).
2. La UI muestra un editor encima y devuelve los tramos nuevos.
3. `replace_paragraph`:
   a. resuelve una fuente para cada tramo (`FontResolver`),
   b. **mide** el resultado en una página temporal (si no cabe, no toca nada),
   c. borra el original con redacción sin tocar imágenes ni gráficos,
   d. escribe con `insert_htmlbox`, corrigiendo el desplazamiento para que la
      primera línea caiga exactamente donde estaba la original.

Coordenadas: PyMuPDF da el texto en el espacio *sin rotar* de la página, pero se
renderiza rotado. `Paragraph.bbox` y demás geometría expuesta a la UI están en
coordenadas **visuales** (las del render); internamente se convierten con
`page.rotation_matrix`.
"""

from __future__ import annotations

import html
import logging
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

import pymupdf

from xpdf.core.errors import XpdfError
from xpdf.core import layout as lay
from xpdf.core.pdffonts import NativeFont, load_native_font
from xpdf.core.fonts import FontRequest, ResolvedFont, classify, clean_font_name

log = logging.getLogger(__name__)

Align = Literal["left", "center", "right", "justify"]
Rect = tuple[float, float, float, float]

# Ligaduras expandidas («ﬁ» -> «fi») para que el texto sea editable con normalidad.
# El guionado de fin de línea se resuelve en `_join_lines`, con más contexto.
_EXTRACT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP

# Bits de span["flags"]
_F_ITALIC = 2
_F_SERIF = 4
_F_MONO = 8
_F_BOLD = 16


class TextEditError(XpdfError):
    """Error de edición de texto con mensaje para el usuario."""


# --------------------------------------------------------------------- fuentes
class FontResolverLike(Protocol):
    """Lo que necesita este módulo de `xpdf.core.fonts.FontResolver` (inyectable en tests)."""

    def resolve(self, req: FontRequest, text: str) -> ResolvedFont: ...

    def missing_glyphs(self, buffer: bytes, text: str) -> set[str]: ...


#: Fuentes de símbolos: sustituirlas cambiaría los glifos por letras. No se editan.
_SYMBOL_HINTS = ("symbol", "dingbat", "wingding", "webding", "zapf", "marlett", "mt extra")


# ---------------------------------------------------------------------- modelo
@dataclass(frozen=True, slots=True)
class TextStyle:
    font: str  # nombre de la fuente tal como aparece en el PDF
    flags: int
    size: float
    color: int  # 0xRRGGBB

    @property
    def bold(self) -> bool:
        return classify(self.font, self.flags).bold

    @property
    def italic(self) -> bool:
        return classify(self.font, self.flags).italic

    @property
    def is_symbol(self) -> bool:
        n = clean_font_name(self.font).lower()
        return any(h in n for h in _SYMBOL_HINTS)

    @property
    def color_rgb(self) -> tuple[int, int, int]:
        return (self.color >> 16) & 255, (self.color >> 8) & 255, self.color & 255


@dataclass(frozen=True, slots=True)
class Run:
    """Tramo de texto con un estilo uniforme."""

    text: str
    style: TextStyle


@dataclass(slots=True)
class _Line:
    bbox: pymupdf.Rect  # visual
    raw_bbox: pymupdf.Rect  # sin rotar
    baseline: float  # visual (y)
    size: float
    runs: list[Run]
    block: int = -1  # nº de bloque de MuPDF (pista de agrupación)
    obs: _Obs | None = None  # métricas observadas en esta línea

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    @property
    def is_rule(self) -> bool:
        """Línea hecha solo de rayas o puntos («_____», «-----», «.....»)."""
        t = self.text.replace(" ", "")
        return len(t) >= 3 and all(c in _RULE_CHARS for c in t)

    @property
    def bold_fraction(self) -> float:
        total = bold = 0
        for r in self.runs:
            n = len(r.text.replace(" ", ""))
            total += n
            if r.style.bold:
                bold += n
        return bold / total if total else 0.0

    def families(self) -> list[str]:
        """Familias tipográficas de la línea, la principal (más caracteres) primero."""
        count: dict[str, int] = {}
        for r in self.runs:
            fam = _family_key(r.style.font)
            count[fam] = count.get(fam, 0) + len(r.text.strip())
        return sorted(count, key=lambda f: -count[f]) or [""]

    def first_word_width(self) -> float:
        """Ancho aproximado de la primera palabra (proporcional a sus caracteres)."""
        text = self.text.strip()
        if not text:
            return 0.0
        first = text.split()[0]
        return float(self.bbox.width * len(first) / len(text))


_RULE_CHARS = set("_-‐‑‒–—―.·•=*~…")


def _family_key(font: str) -> str:
    """«ABCDEF+EquityTextA-Italic» -> «equitytexta»; «Arial,Bold» -> «arial»."""
    name = clean_font_name(font).lower()
    for sep in ("-", ","):
        name = name.split(sep)[0]
    for suffix in ("psmt", "mt", "ps"):
        if name.endswith(suffix) and len(name) > len(suffix) + 2:
            name = name[: -len(suffix)]
    return name.replace(" ", "")


@dataclass(slots=True)
class Paragraph:
    page_index: int
    lines: list[_Line]
    align: Align = "left"
    line_pitch: float = 0.0  # distancia entre líneas base (pt)
    indent: float = 0.0  # sangría de primera línea (pt, puede ser negativa)
    id: int = 0
    #: Si no es None, el párrafo no se puede editar con garantías (motivo para el usuario).
    blocked_reason: str | None = None
    #: Ancho disponible (x0, x1) visual para recomponer. Para varias líneas es el
    #: del propio párrafo; una línea suelta puede crecer hasta el margen o hasta
    #: el siguiente contenido, según su alineación (como en Acrobat).
    box: tuple[float, float] = (0.0, 0.0)
    _runs: list[Run] = field(default_factory=list)
    _metrics: Metrics | None = None

    @property
    def metrics(self) -> Metrics:
        """Métricas reales del texto original de este párrafo."""
        if self._metrics is None:
            obs = [ln.obs for ln in self.lines if ln.obs is not None]
            # En justificado, los huecos entre palabras están estirados salvo en la
            # última línea: solo esa da el hueco natural.
            gap_from = (
                [self.lines[-1].obs] if self.align == "justify" and self.lines[-1].obs else obs
            )
            self._metrics = Metrics.from_obs(obs, [o for o in gap_from if o is not None])
        return self._metrics

    @property
    def bbox(self) -> Rect:
        r = pymupdf.Rect(self.lines[0].bbox)
        for ln in self.lines[1:]:
            r |= ln.bbox
        return (r.x0, r.y0, r.x1, r.y1)

    @property
    def line_rects(self) -> list[Rect]:
        return [(ln.bbox.x0, ln.bbox.y0, ln.bbox.x1, ln.bbox.y1) for ln in self.lines]

    @property
    def first_baseline(self) -> float:
        return self.lines[0].baseline

    @property
    def size(self) -> float:
        return _dominant_size(self.lines)

    @property
    def runs(self) -> list[Run]:
        if not self._runs:
            self._runs = _join_lines(self.lines)
        return self._runs

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass(frozen=True, slots=True)
class EditResult:
    bbox: Rect  # caja visual del texto nuevo
    #: (fuente original, fuente usada, caracteres afectados; "" = todo el tramo)
    substitutions: tuple[tuple[str, str, str], ...]
    #: El texto nuevo se monta sobre otro contenido de texto de la página.
    overlaps: bool = False


# ----------------------------------------------------------------- utilidades
def _dominant_size(lines: Sequence[_Line]) -> float:
    weights: dict[float, int] = {}
    for ln in lines:
        for r in ln.runs:
            weights[round(r.style.size, 1)] = weights.get(round(r.style.size, 1), 0) + len(
                r.text.strip()
            )
    if not weights:
        return lines[0].size if lines else 12.0
    return max(weights, key=lambda k: weights[k])


def _merge_runs(runs: list[Run]) -> list[Run]:
    out: list[Run] = []
    for r in runs:
        if not r.text:
            continue
        if out and out[-1].style == r.style:
            out[-1] = Run(out[-1].text + r.text, r.style)
        else:
            out.append(r)
    return out


def _join_lines(lines: Sequence[_Line]) -> list[Run]:
    """Une las líneas en un texto continuo, resolviendo guiones de partición."""
    runs: list[Run] = []
    for i, ln in enumerate(lines):
        line_runs = list(ln.runs)
        if i > 0 and runs:
            prev = runs[-1]
            nxt = ln.text.lstrip()
            ptxt = prev.text.rstrip(" ")
            if ptxt.endswith(("-", "­")) and nxt[:1].islower() and len(ptxt) > 1 and ptxt[-2].isalpha():
                # Guion de partición de palabra: se elimina y se une sin espacio.
                runs[-1] = Run(ptxt[:-1], prev.style)
            elif not prev.text.endswith((" ", " ")) and not ln.text.startswith(" "):
                runs[-1] = Run(prev.text + " ", prev.style)
            # quitar espacios iniciales de la línea siguiente
            if line_runs:
                line_runs[0] = Run(line_runs[0].text.lstrip(" "), line_runs[0].style)
        runs.extend(line_runs)
    merged = _merge_runs(runs)
    if merged:
        merged[-1] = Run(merged[-1].text.rstrip(" "), merged[-1].style)
    return [r for r in merged if r.text]


def _spread(values: Sequence[float]) -> float:
    return max(values) - min(values) if values else 0.0


# ------------------------------------------------------------------ detección
@dataclass(slots=True)
class _Obs:
    """Medidas en bruto de una línea del original, en em."""

    adv: dict[tuple[str, str], list[float]] = field(default_factory=dict)  # (fuente, car.)
    pair: dict[tuple[str, str, str], list[float]] = field(default_factory=dict)  # (f, c1, c2)
    gap: dict[str, list[float]] = field(default_factory=dict)  # fuente -> hueco entre palabras

    def merge(self, other: _Obs) -> None:
        for ka, va in other.adv.items():
            self.adv.setdefault(ka, []).extend(va)
        for kp, vp in other.pair.items():
            self.pair.setdefault(kp, []).extend(vp)
        for kg, vg in other.gap.items():
            self.gap.setdefault(kg, []).extend(vg)


@dataclass(slots=True)
class Metrics:
    """Métricas reales del original, en em, para reescribir sin que nada se mueva.

    El PDF declara sus propios anchos de glifo (`/W`, que Word redondea) que no
    siempre coinciden con los del programa de fuente; el generador pudo aplicar
    espaciado entre caracteres (`Tc`), entre palabras (`Tw`) y kerning. Todo eso
    se mide en el original y se reproduce al escribir con la misma fuente.
    """

    advance: dict[tuple[str, str], float] = field(default_factory=dict)
    spacing: dict[str, float] = field(default_factory=dict)  # Tc por fuente
    kerning: dict[tuple[str, str, str], float] = field(default_factory=dict)  # sobre `spacing`
    gap: dict[str, float] = field(default_factory=dict)  # hueco entre palabras por fuente

    @classmethod
    def from_obs(cls, obs: Sequence[_Obs], gap_obs: Sequence[_Obs]) -> Metrics:
        acc = _Obs()
        for o in obs:
            acc.merge(o)
        m = cls()
        m.advance = {k: statistics.median(v) for k, v in acc.adv.items()}
        by_font: dict[str, list[float]] = {}
        for (font, _, _), vals in acc.pair.items():
            by_font.setdefault(font, []).extend(vals)
        m.spacing = {f: statistics.median(v) for f, v in by_font.items()}
        for key, vals in acc.pair.items():
            extra = statistics.median(vals) - m.spacing.get(key[0], 0.0)
            if abs(extra) > 0.002:  # por debajo es ruido de redondeo
                m.kerning[key] = extra
        gaps = _Obs()
        for o in gap_obs:
            gaps.merge(o)
        m.gap = {f: statistics.median(v) for f, v in gaps.gap.items() if v}
        return m

    def pair_adjust(self, font: str, c1: str, c2: str) -> float:
        return self.spacing.get(font, 0.0) + self.kerning.get((font, c1, c2), 0.0)


def _observe_line(spans: Sequence[dict[str, object]], dir_x: float, dir_y: float) -> _Obs:
    """Avances, ajustes entre caracteres y huecos entre palabras de una línea."""
    horizontal = abs(dir_x) >= abs(dir_y)
    seq: list[tuple[str, float, float, str, float]] = []  # (car, pos, avance, fuente, tamaño)
    for sp in spans:
        size = float(sp["size"])  # type: ignore[arg-type]
        if size <= 0:
            continue
        for ch in sp["chars"]:  # type: ignore[attr-defined]
            box = pymupdf.Rect(ch["bbox"])
            ox, oy = ch["origin"]
            seq.append(
                (ch["c"], ox * dir_x + oy * dir_y, box.width if horizontal else box.height,
                 str(sp["font"]), size)
            )
    obs = _Obs()
    for c, _, adv, font, size in seq:
        if c.strip():
            obs.adv.setdefault((font, c), []).append(adv / size)
    for (c1, p1, a1, f1, s1), (c2, p2, _, f2, _) in zip(seq, seq[1:]):
        if c1.strip() and c2.strip() and f1 == f2:
            obs.pair.setdefault((f1, c1, c2), []).append((p2 - p1 - a1) / s1)
    # Hueco entre palabras: del final de la última letra al origen de la siguiente.
    i = 0
    while i < len(seq):
        if seq[i][0].strip():
            i += 1
            continue
        j = i
        while j < len(seq) and not seq[j][0].strip():
            j += 1
        if i > 0 and j < len(seq) and j - i == 1:
            c1, p1, a1, f1, s1 = seq[i - 1]
            gap_font = seq[i][3]
            obs.gap.setdefault(gap_font, []).append((seq[j][1] - p1 - a1) / s1)
        i = j
    return obs


def _extract_lines(page: pymupdf.Page) -> list[_Line]:
    m = page.rotation_matrix
    data = page.get_text("rawdict", flags=_EXTRACT_FLAGS)
    lines: list[_Line] = []
    for bno, block in enumerate(data["blocks"]):
        if block.get("type", 0) != 0:
            continue
        for ln in block["lines"]:
            if ln.get("wmode", 0) != 0:
                continue
            dx, dy = ln["dir"]
            vx, vy = dx * m.a + dy * m.c, dx * m.b + dy * m.d
            if vx < 0.99 or abs(vy) > 0.02:
                continue  # solo texto que se ve horizontal y de izquierda a derecha
            runs: list[Run] = []
            baseline = None
            sizes: list[float] = []
            for sp in ln["spans"]:
                sp["text"] = "".join(ch["c"] for ch in sp["chars"])
                if not sp["text"]:
                    continue
                if sp.get("alpha", 255) == 0:
                    continue  # texto invisible (capa OCR): no se edita aquí
                style = TextStyle(sp["font"], int(sp["flags"]), float(sp["size"]), int(sp["color"]))
                runs.append(Run(sp["text"], style))
                sizes.append(float(sp["size"]))
                if baseline is None and sp["text"].strip():
                    baseline = (pymupdf.Point(sp["origin"]) * m).y
            runs = _merge_runs(runs)
            if not runs or not "".join(r.text for r in runs).strip() or baseline is None:
                continue
            raw = pymupdf.Rect(ln["bbox"])
            obs = _observe_line(ln["spans"], dx, dy)
            lines.append(_Line(raw * m, raw, baseline, max(sizes), runs, bno, obs))
    return lines


def _merged_obs(a: _Obs | None, b: _Obs | None) -> _Obs | None:
    if a is None or b is None:
        return a or b
    out = _Obs()
    out.merge(a)
    out.merge(b)
    return out


def _merge_same_baseline(lines: list[_Line]) -> list[_Line]:
    """Une fragmentos de una misma línea visual.

    MuPDF parte una línea cuando hay huecos grandes (justificado con espacios
    muy estirados, tabuladores, kerning raro). Dentro del mismo bloque de MuPDF se
    admiten huecos amplios; entre bloques distintos (columnas, celdas) solo
    huecos del tamaño de un espacio normal.
    """
    rows: list[list[_Line]] = []
    for ln in sorted(lines, key=lambda l: l.baseline):
        if rows and abs(rows[-1][0].baseline - ln.baseline) <= 0.2 * max(rows[-1][0].size, ln.size):
            rows[-1].append(ln)
        else:
            rows.append([ln])
    out: list[_Line] = []
    for row in rows:
        row.sort(key=lambda l: l.bbox.x0)
        cur = row[0]
        for ln in row[1:]:
            size = max(cur.size, ln.size)
            gap = ln.bbox.x0 - cur.bbox.x1
            limit = 4.0 * size if ln.block == cur.block else 1.2 * size
            if -1.0 <= gap <= limit:
                sep: list[Run] = []
                if gap > 0.15 * ln.size and not cur.text.endswith(" ") and not ln.text.startswith(" "):
                    sep = [Run(" ", cur.runs[-1].style)]
                cur = _Line(
                    cur.bbox | ln.bbox,
                    cur.raw_bbox | ln.raw_bbox,
                    cur.baseline,
                    size,
                    _merge_runs(cur.runs + sep + ln.runs),
                    cur.block,
                    _merged_obs(cur.obs, ln.obs),
                )
            else:
                out.append(cur)
                cur = ln
        out.append(cur)
    return out


def _fits_paragraph(p: Paragraph, ln: _Line, col_right: dict[int, float] | None = None) -> bool:
    last = p.lines[-1]
    if last.is_rule or ln.is_rule:
        return False  # una raya de subrayado no es parte del texto de arriba ni de abajo
    if abs(last.bold_fraction - ln.bold_fraction) > 0.8:
        return False  # línea entera en negrita frente a texto normal: título y cuerpo
    fam_last, fam_ln = last.families(), ln.families()
    if fam_last[0] != fam_ln[0] and fam_last[0] not in fam_ln and fam_ln[0] not in fam_last:
        return False  # otra familia tipográfica: otro bloque (sello, cabecera, nota...)
    size = max(_dominant_size(p.lines), _dominant_size([ln]))
    if abs(_dominant_size(p.lines) - _dominant_size([ln])) > 0.2 * size:
        return False
    step = ln.baseline - last.baseline
    if not (0.6 * size <= step <= 2.0 * size):
        return False
    if len(p.lines) >= 2:
        pitch = p.lines[-1].baseline - p.lines[-2].baseline
        if abs(step - pitch) > 0.25 * size:
            return False
    # solapamiento horizontal
    bx0, _, bx1, _ = p.bbox
    overlap = min(bx1, ln.bbox.x1) - max(bx0, ln.bbox.x0)
    if overlap < 0.5 * min(bx1 - bx0, ln.bbox.width):
        return False
    tol = max(1.5, 0.25 * size)
    lefts_ok = abs(ln.bbox.x0 - (p.lines[1].bbox.x0 if len(p.lines) > 1 else last.bbox.x0)) <= tol
    indent_ok = len(p.lines) == 1 and 0 < last.bbox.x0 - ln.bbox.x0 <= 6 * size
    hanging_ok = len(p.lines) == 1 and 0 < ln.bbox.x0 - last.bbox.x0 <= 4 * size and ln.bbox.x0 > last.bbox.x0
    centered_ok = abs((ln.bbox.x0 + ln.bbox.x1) / 2 - (last.bbox.x0 + last.bbox.x1) / 2) <= tol
    rights_ok = abs(ln.bbox.x1 - last.bbox.x1) <= tol
    if (lefts_ok or indent_ok or hanging_ok) and not centered_ok and not rights_ok and col_right is not None:
        # Salto forzado: si la primera palabra de `ln` cabía al final de la línea
        # anterior, esa línea terminó a propósito (fin de párrafo o salto manual).
        right = max(col_right.get(id(last), last.bbox.x1), col_right.get(id(ln), ln.bbox.x1))
        if last.bbox.x1 + 0.3 * size + ln.first_word_width() < right - tol:
            return False
    return lefts_ok or indent_ok or hanging_ok or centered_ok or rights_ok


def _column_rights(lines: Sequence[_Line]) -> dict[int, float]:
    """Para cada línea, el borde derecho de su columna: el máximo de las líneas
    que empiezan en el mismo margen izquierdo."""
    out: dict[int, float] = {}
    for ln in lines:
        same = [o.bbox.x1 for o in lines if abs(o.bbox.x0 - ln.bbox.x0) <= 2.0]
        out[id(ln)] = max(same)
    return out


def _text_margins(lines: Sequence[_Line]) -> tuple[float, float]:
    """Márgenes de la columna de texto principal: el izquierdo más común y el
    borde derecho de las líneas que empiezan en él (no sellos ni cabeceras que
    sobresalen)."""
    left = min(o.bbox.x0 for o in lines)
    body = [o.bbox.x1 for o in lines if abs(o.bbox.x0 - left) <= 2.0]
    return left, max(body) if body else max(o.bbox.x1 for o in lines)


def _edit_box(p: Paragraph, lines: Sequence[_Line], page_rect: pymupdf.Rect) -> tuple[float, float]:
    """Ancho disponible para recomponer el párrafo (ver `Paragraph.box`)."""
    x0, y0, x1, y1 = p.bbox
    if len(p.lines) > 1:
        body = min(ln.bbox.x0 for ln in p.lines[1:]) if p.indent else x0
        return body, x1
    own = {id(ln) for ln in p.lines}
    others = [o for o in lines if id(o) not in own]
    left_margin, right_margin = _text_margins(lines)
    left_margin, right_margin = min(left_margin, x0), max(right_margin, x1)
    gap = 0.5 * p.size
    band = [o for o in others if o.bbox.y1 > y0 + 1 and o.bbox.y0 < y1 - 1]
    right_limit = min([o.bbox.x0 - gap for o in band if o.bbox.x0 >= x1 - 1] + [right_margin])
    left_limit = max([o.bbox.x1 + gap for o in band if o.bbox.x1 <= x0 + 1] + [left_margin])
    right_limit = max(min(right_limit, page_rect.x1), x1)
    left_limit = min(max(left_limit, page_rect.x0), x0)
    if p.align == "right":
        return left_limit, x1
    if p.align == "center":
        c = (x0 + x1) / 2
        half = max(min(c - left_limit, right_limit - c), (x1 - x0) / 2)
        return c - half, c + half
    return x0, right_limit


def _classify_single(
    p: Paragraph, lines: Sequence[_Line], page_x0: float = 0.0, page_x1: float = 0.0
) -> None:
    """Alineación de una línea suelta, por su posición respecto al texto de la página."""
    ln = p.lines[0]
    left_m, right_m = _text_margins(lines)
    tol = max(2.0, 0.25 * p.size)
    if ln.bbox.x0 - left_m <= tol:
        return  # empieza en el margen: izquierda
    center = (ln.bbox.x0 + ln.bbox.x1) / 2
    # Con texto en bandera el borde derecho medido queda algo por dentro del
    # margen real: el centro admite un margen proporcional al ancho de columna.
    page_center = (page_x0 + page_x1) / 2
    if abs(center - (left_m + right_m) / 2) <= max(tol, 0.02 * (right_m - left_m)) or (
        abs(center - page_center) <= tol
    ):
        p.align = "center"
    elif abs(ln.bbox.x1 - right_m) <= tol:
        p.align = "right"


def _classify(p: Paragraph) -> None:
    lines = p.lines
    size = p.size
    tol = max(1.5, 0.2 * size)
    if len(lines) >= 2:
        diffs = [b.baseline - a.baseline for a, b in zip(lines, lines[1:])]
        p.line_pitch = statistics.median(diffs)
    else:
        p.line_pitch = 1.2 * size
    if len(lines) == 1:
        p.align = "left"
        return
    lefts = [ln.bbox.x0 for ln in lines]
    rights = [ln.bbox.x1 for ln in lines]
    centers = [(a + b) / 2 for a, b in zip(lefts, rights)]
    body_lefts = lefts[1:] if len(lines) > 1 else lefts
    left_ok = _spread(body_lefts) <= tol
    if left_ok:
        p.indent = lefts[0] - min(body_lefts)
        if abs(p.indent) <= tol:
            p.indent = 0.0
    right_body_ok = _spread(rights[:-1]) <= tol
    if left_ok and len(lines) >= 3 and right_body_ok and rights[-1] < max(rights[:-1]) + tol:
        p.align = "justify"
    elif left_ok and len(lines) == 2 and right_body_ok and _looks_justified(lines[0]):
        p.align = "justify"
    elif left_ok:
        p.align = "left"
    elif _spread(centers) <= tol:
        p.align, p.indent = "center", 0.0
    elif _spread(rights) <= tol:
        p.align, p.indent = "right", 0.0
    else:
        p.align = "left"
        p.indent = 0.0


def _looks_justified(line: _Line) -> bool:
    """Con dos líneas no basta la geometría: mira si los espacios están estirados."""
    text = line.text
    n_spaces = text.count(" ")
    if n_spaces < 2:
        return False
    avg = line.bbox.width / max(len(text), 1)
    return bool(avg > 0.62 * line.size)  # heurística: espacio medio muy por encima de lo normal


_MATH_FONT_HINTS = ("math", "cmmi", "cmex", "msam", "msbm", "esint", "stmary")


def _has_math(p: Paragraph) -> bool:
    """Fórmulas: letras matemáticas Unicode o fuentes de matemáticas (TeX, Cambria Math...)."""
    if any(0x1D400 <= ord(c) <= 0x1D7FF for c in p.text):
        return True
    return any(
        any(h in clean_font_name(r.style.font).lower() for h in _MATH_FONT_HINTS) for r in p.runs
    )


def detect_paragraphs(doc: pymupdf.Document, page_index: int) -> list[Paragraph]:
    page = doc[page_index]
    lines = _merge_same_baseline(_extract_lines(page))
    lines.sort(key=lambda l: (l.baseline, l.bbox.x0))
    col_right = _column_rights(lines)
    paras: list[Paragraph] = []
    open_paras: list[Paragraph] = []
    for ln in lines:
        best: Paragraph | None = None
        best_dist = 1e9
        for p in open_paras:
            if _fits_paragraph(p, ln, col_right):
                dist = ln.baseline - p.lines[-1].baseline
                if dist < best_dist:
                    best, best_dist = p, dist
        if best is None:
            best = Paragraph(page_index, [ln])
            paras.append(best)
            open_paras.append(best)
        else:
            best.lines.append(ln)
        # cerrar párrafos que ya quedan muy por encima
        open_paras = [p for p in open_paras if ln.baseline - p.lines[-1].baseline <= 3 * p.size]
    paras.sort(key=lambda p: (p.bbox[1], p.bbox[0]))
    type3 = {
        clean_font_name(str(f[3])) for f in page.get_fonts(full=True) if f[2] == "Type3"
    }
    for i, p in enumerate(paras):
        p.id = i
        _classify(p)
        if len(p.lines) == 1:
            _classify_single(p, lines, page.rect.x0, page.rect.x1)
        p.box = _edit_box(p, lines, page.rect)
        styles = {r.style for r in p.runs}
        if any(st.is_symbol for st in styles):
            p.blocked_reason = (
                "Este párrafo usa una fuente de símbolos; editarlo cambiaría los símbolos por letras."
            )
        elif _has_math(p):
            p.blocked_reason = (
                "Este párrafo contiene fórmulas matemáticas; xpdf todavía no puede "
                "editarlas sin estropear sus símbolos."
            )
        elif any(clean_font_name(st.font) in type3 or st.font.startswith("Type3") for st in styles):
            p.blocked_reason = (
                "Este párrafo usa una fuente Type3 (dibujada a mano), que no se puede reutilizar "
                "ni sustituir con fidelidad."
            )
    return paras


def paragraph_at(paras: Sequence[Paragraph], x: float, y: float, margin: float = 2.0) -> Paragraph | None:
    for p in paras:
        x0, y0, x1, y1 = p.bbox
        if x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin:
            return p
    return None


# --------------------------------------------------------------------- escritura
def _css_color(c: int) -> str:
    return f"#{c & 0xFFFFFF:06x}"


def _build_html(
    runs: Sequence[Run],
    fonts: dict[TextStyle, ResolvedFont],
    align: Align,
    line_pitch: float,
    indent: float,
) -> tuple[str, str]:
    faces: dict[str, str] = {}
    spans: list[str] = []
    for r in runs:
        f = fonts[r.style]
        faces[f.css_family] = (
            f"@font-face {{font-family: '{f.css_family}'; src: url('{f.filename}');"
            f" font-weight: {'bold' if f.bold else 'normal'};"
            f" font-style: {'italic' if f.italic else 'normal'};}}"
        )
        weight = "bold" if (r.style.bold or f.bold) else "normal"
        style = "italic" if (r.style.italic or f.italic) else "normal"
        text = html.escape(r.text).replace("\n", "<br/>")
        spans.append(
            f"<span style=\"font-family: '{f.css_family}'; font-size: {r.style.size:.2f}px;"
            f" color: {_css_color(r.style.color)}; font-weight: {weight}; font-style: {style};\">"
            f"{text}</span>"
        )
    css = "\n".join(faces.values()) + (
        "\n* {margin: 0; padding: 0;}"
        f"\np {{text-align: {align}; line-height: {line_pitch:.3f}px; text-indent: {indent:.2f}px;"
        " white-space: pre-wrap;}"
    )
    return f"<p>{''.join(spans)}</p>", css


def _resolve_fonts(
    runs: Sequence[Run], resolver: FontResolverLike, page_index: int
) -> dict[TextStyle, ResolvedFont]:
    texts: dict[TextStyle, str] = {}
    for r in runs:
        texts[r.style] = texts.get(r.style, "") + r.text
    return {
        st: resolver.resolve(FontRequest(st.font, st.flags, page_index), txt)
        for st, txt in texts.items()
    }


def _write_box(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    html_text: str,
    css: str,
    fonts: dict[TextStyle, ResolvedFont],
) -> float:
    arch = pymupdf.Archive()
    added: set[str] = set()
    for f in fonts.values():
        if f.filename not in added:
            arch.add(f.buffer, f.filename)
            added.add(f.filename)
    spare, _ = page.insert_htmlbox(
        rect, html_text, css=css, archive=arch, scale_low=1, rotate=page.rotation
    )
    return float(spare)


@dataclass(frozen=True, slots=True)
class _Metrics:
    baseline: float  # línea base visual de la primera línea
    x0: float  # extremos visuales de la primera línea
    x1: float
    n_lines: int
    bbox: pymupdf.Rect  # caja visual de todo el texto
    widths: tuple[float, ...] = ()  # ancho de cada línea, de arriba abajo


def _first_line_metrics(page: pymupdf.Page) -> _Metrics | None:
    lines = _merge_same_baseline(_extract_lines(page))
    if not lines:
        return None
    lines.sort(key=lambda l: l.baseline)
    first = lines[0]
    total = pymupdf.Rect(first.bbox)
    for ln in lines[1:]:
        total |= ln.bbox
    widths = tuple(ln.bbox.width for ln in lines)
    return _Metrics(first.baseline, first.bbox.x0, first.bbox.x1, len(lines), total, widths)


def _visual_to_raw(page: pymupdf.Page, r: pymupdf.Rect) -> pymupdf.Rect:
    return pymupdf.Rect(r * ~page.rotation_matrix)


def _measure(
    tp: pymupdf.Page, rect: pymupdf.Rect, html_text: str, css: str, fonts: dict[TextStyle, ResolvedFont]
) -> _Metrics | None:
    """Escribe en la página temporal (vaciada antes) y mide el resultado."""
    for xref in tp.get_contents():
        tp.parent.update_stream(xref, b"")
    spare = _write_box(tp, _visual_to_raw(tp, rect), html_text, css, fonts)
    if spare < 0:
        raise TextEditError("El texto no cabe en la página. Acórtalo o reduce el tamaño de letra.")
    return _first_line_metrics(tp)


#: Holguras (izquierda, derecha) a probar, por alineación. Se añaden por el lado
#: que no desplaza el texto: derecha si va a la izquierda, izquierda si va a la
#: derecha, ambos si va centrado. En justificado las líneas se estiran hasta el
#: borde, así que ahí la holgura es mínima. La primera que reproduce el original
#: es la que se usa.
_SLACKS: dict[Align, tuple[tuple[float, float], ...]] = {
    "left": ((0.0, 1.0), (0.0, 2.0), (0.0, 3.0), (0.0, 0.3)),
    "right": ((2.0, 0.0), (3.0, 0.0), (4.0, 0.0), (1.0, 0.0)),
    "center": ((2.0, 2.0), (3.0, 3.0), (4.0, 4.0), (1.0, 1.0)),
    "justify": ((0.0, 0.01), (0.0, 0.3), (0.0, 0.6), (0.0, 1.0)),
}


@dataclass(frozen=True, slots=True)
class _Target:
    """Cómo era el original: lo que la caja calibrada debe reproducir."""

    html: str
    css: str
    baseline: float
    first_x0: float
    first_x1: float
    widths: tuple[float, ...]  # ancho de cada línea original

    def same_breaks(self, m: _Metrics) -> bool:
        """¿Se partieron las líneas igual que en el original?"""
        return len(m.widths) == len(self.widths) and all(
            abs(a - b) <= 1.0 for a, b in zip(m.widths, self.widths)
        )


def _corrected_rect(
    rect: pymupdf.Rect, m: _Metrics, align: Align, target: _Target, bottom: float
) -> pymupdf.Rect:
    """Desplaza la caja para que la primera línea medida coincida con la original."""
    dy = target.baseline - m.baseline
    left_fix = target.first_x0 - m.x0
    right_fix = target.first_x1 - m.x1
    if align == "left":
        dl, dr = left_fix, -left_fix  # margen interior simétrico
    elif align == "justify":
        dl = left_fix
        # el borde derecho solo es fiable si la primera línea no es la última
        dr = right_fix if m.n_lines > 1 else -left_fix
    elif align == "right":
        dl = dr = right_fix
    else:  # center
        dl = dr = ((target.first_x0 + target.first_x1) - (m.x0 + m.x1)) / 2
    # Son márgenes interiores del motor HTML: pequeños, o algo va mal.
    dl = dl if abs(dl) <= 4 else 0.0
    dr = dr if abs(dr) <= 4 else 0.0
    return pymupdf.Rect(rect.x0 + dl, rect.y0 + dy, rect.x1 + dr, bottom)


def _calibrate_box(
    tp: pymupdf.Page,
    x0: float,
    x1: float,
    top: float,
    fonts: dict[TextStyle, ResolvedFont],
    align: Align,
    target: _Target,
) -> pymupdf.Rect:
    """Busca la caja en la que el texto *original* se compone igual que en el PDF.

    El motor HTML deja márgenes interiores que dependen de la fuente, y coloca la
    primera línea base según sus métricas. En vez de suponerlos, se mide: se
    compone el texto original (cuyas líneas y posiciones conocemos) en una página
    temporal y se corrige la caja hasta reproducirlo. Después el texto nuevo se
    escribe en esa misma caja.
    """
    bottom = tp.rect.y1
    best: pymupdf.Rect | None = None
    for slack_l, slack_r in _SLACKS[align]:
        rect = pymupdf.Rect(x0 - slack_l, top, x1 + slack_r, bottom)
        if rect.width < 1 or rect.height < 1:
            raise TextEditError("No hay espacio en la página para colocar el texto.")
        m1 = _measure(tp, rect, target.html, target.css, fonts)
        if m1 is None:
            return rect
        rect = _corrected_rect(rect, m1, align, target, bottom)
        m2 = _measure(tp, rect, target.html, target.css, fonts)
        if m2 is None:
            return rect
        if abs(m2.baseline - target.baseline) > 0.05:  # métricas raras: un ajuste más
            rect = pymupdf.Rect(rect.x0, rect.y0 + target.baseline - m2.baseline, rect.x1, bottom)
        if best is None:
            best = rect
        if target.same_breaks(m2):
            return rect
    log.info("No se pudo reproducir el ajuste de líneas original; se usa la caja más próxima")
    assert best is not None
    return best


def _place(
    page: pymupdf.Page,
    x0: float,
    x1: float,
    top: float,
    html_text: str,
    css: str,
    fonts: dict[TextStyle, ResolvedFont],
    align: Align,
    target: _Target | None,
    baseline: float,
) -> tuple[pymupdf.Rect, pymupdf.Rect]:
    """Calcula la caja final y mide el texto nuevo en ella (sin tocar `page`).

    Devuelve (rect visual de la caja, bbox visual del texto nuevo). Si el texto no
    cabe en la página, lanza `TextEditError`.
    """
    tmp = pymupdf.open()
    try:
        tp = tmp.new_page(width=page.mediabox.width, height=page.mediabox.height)
        tp.set_rotation(page.rotation)
        bottom = tp.rect.y1
        if target is not None:
            rect = _calibrate_box(tp, x0, x1, top, fonts, align, target)
        else:
            rect = pymupdf.Rect(x0, top, x1, bottom)
            m = _measure(tp, rect, html_text, css, fonts)
            if m is not None:
                fake = _Target(html_text, css, baseline, x0, m.x1, m.widths)
                rect = _corrected_rect(rect, m, "left", fake, bottom)
        m = _measure(tp, rect, html_text, css, fonts)
        bbox = m.bbox if m is not None else pymupdf.Rect(x0, top, x0, top)
        return rect, bbox
    finally:
        tmp.close()


def redact_lines(page: pymupdf.Page, lines: Sequence[_Line]) -> None:
    """Borra solo los glifos de las líneas indicadas.

    La redacción de MuPDF elimina cualquier carácter cuyo rectángulo toque la zona,
    así que se usa una franja fina en la línea base media de cada línea: no alcanza
    a las líneas vecinas aunque sus cajas se solapen.
    """
    for ln in lines:
        mid = ln.baseline - 0.3 * ln.size
        band = pymupdf.Rect(ln.bbox.x0 + 0.2, mid - 0.5, ln.bbox.x1 - 0.2, mid + 0.5)
        # fill=False: sin recuadro de relleno, el fondo queda intacto (verificado en tests).
        page.add_redact_annot(_visual_to_raw(page, band), fill=False)  # type: ignore[arg-type]
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )


# ------------------------------------------------------- composición propia
_CJK_FALLBACK: pymupdf.Font | None = None


def _cjk_font() -> pymupdf.Font:
    global _CJK_FALLBACK
    if _CJK_FALLBACK is None:
        _CJK_FALLBACK = pymupdf.Font("cjk")
    return _CJK_FALLBACK


class _FontPicker:
    """Elige la fuente de cada carácter.

    1. La fuente principal del estilo: se resuelve con el texto *original* de ese
       estilo, así que si la fuente incrustada lo dibujaba, se reutiliza.
    2. Para los caracteres nuevos que le falten, una fuente de respaldo del mismo
       estilo (sistema o incluida), solo para esos caracteres: el resto del
       párrafo conserva su aspecto.
    3. Como último recurso, la fuente CJK integrada en MuPDF.
    """

    def __init__(
        self,
        resolver: FontResolverLike,
        page_index: int,
        original: dict[TextStyle, str],
        natives: dict[str, NativeFont] | None = None,
    ) -> None:
        self._resolver = resolver
        self._page = page_index
        self._original = original
        #: Fuentes originales del PDF por nombre de fuente del tramo: se escribe
        #: con ellas todo lo que ya dibujan (mismo aspecto y métricas exactas).
        self.natives = natives or {}
        self._fonts: dict[str, pymupdf.Font] = {}
        self._primary: dict[TextStyle, ResolvedFont] = {}
        #: (original, usada) -> caracteres sustituidos; vacío = todo el tramo.
        self.substitutions: dict[tuple[str, str], set[str]] = {}

    def font(self, rf: ResolvedFont) -> pymupdf.Font:
        f = self._fonts.get(rf.filename)
        if f is None:
            f = self._fonts[rf.filename] = pymupdf.Font(fontbuffer=rf.buffer)
        return f

    def _req(self, st: TextStyle) -> FontRequest:
        return FontRequest(st.font, st.flags, self._page)

    def primary(self, st: TextStyle, text: str) -> ResolvedFont:
        rf = self._primary.get(st)
        if rf is None:
            basis = self._original.get(st) or text
            rf = self._primary[st] = self._resolver.resolve(self._req(st), basis)
        return rf

    def assign(self, st: TextStyle, text: str) -> list[tuple[str, lay.AnyFont]]:
        """Divide `text` en tramos (texto, fuente) según la cobertura de glifos."""
        nat = self.natives.get(st.font)
        if nat is None:
            return list(self._assign_regular(st, text))
        rest = "".join(c for c in text if c not in nat.covered and c.strip())
        regular = {c: f for piece, f in self._assign_regular(st, rest) for c in piece} if rest else {}
        out: list[tuple[str, lay.AnyFont]] = []
        prim = self._primary.get(st)
        same_program = self.font(prim) if prim is not None and prim.source == "embedded" else None
        for ch in text:
            f: lay.AnyFont = nat if (ch in nat.covered or not ch.strip()) else regular.get(ch, nat)
            # Con el mismo programa de fuente incrustado no hay sustitución que avisar.
            if f is not nat and f is not same_program:
                chars = self.substitutions.setdefault((nat.name, str(getattr(f, "name", ""))), {ch})
                if chars:
                    chars.add(ch)
            if out and out[-1][1] is f:
                out[-1] = (out[-1][0] + ch, f)
            else:
                out.append((ch, f))
        return out

    def _assign_regular(self, st: TextStyle, text: str) -> list[tuple[str, pymupdf.Font]]:
        prim = self.primary(st, text)
        chosen: dict[str, tuple[pymupdf.Font, ResolvedFont | None]] = {}
        missing = {c for c in self._resolver.missing_glyphs(prim.buffer, text) if c.strip()}
        if missing:
            sec = self._resolver.resolve(self._req(st), "".join(sorted(missing)))
            still = {c for c in self._resolver.missing_glyphs(sec.buffer, "".join(missing)) if c.strip()}
            for c in missing - still:
                chosen[c] = (self.font(sec), sec)
            cjk = _cjk_font()
            for c in still:
                chosen[c] = (cjk, None) if cjk.has_glyph(ord(c)) else (self.font(prim), prim)
        pf = self.font(prim)
        if prim.substituted and st.font not in self.natives:
            self.substitutions[(prim.original_name, pf.name)] = set()  # todo el tramo
        out: list[tuple[str, pymupdf.Font]] = []
        for ch in text:
            f, rf = chosen.get(ch, (pf, prim))
            if f is not pf:
                chars = self.substitutions.setdefault((prim.original_name, f.name), {ch})
                if chars:
                    chars.add(ch)
            if out and out[-1][1] is f:
                out[-1] = (out[-1][0] + ch, f)
            else:
                out.append((ch, f))
        return out


def _tokens(
    runs: Sequence[Run], picker: _FontPicker, metrics: Metrics | None = None
) -> list[lay.Token]:
    toks: list[lay.Token] = []
    for r in runs:
        st = r.style
        nat = picker.natives.get(st.font)
        # Las métricas observadas solo valen con la misma fuente del original.
        prim = picker.primary(st, r.text) if nat is None else None
        reuse = metrics is not None and (nat is not None or (prim is not None and prim.source == "embedded"))
        prim_font: lay.AnyFont = nat if nat is not None else picker.font(prim)  # type: ignore[arg-type]

        def advances(text: str, font: lay.AnyFont) -> tuple[float, ...]:
            if isinstance(font, NativeFont):
                # Avances del propio PDF (/W) más el espaciado/kerning medido.
                out_n: list[float] = []
                for i, ch in enumerate(text):
                    adv = font.glyph_advance(ord(ch)) * st.size
                    if metrics is not None and i + 1 < len(text):
                        adv += metrics.pair_adjust(st.font, ch, text[i + 1]) * st.size
                    out_n.append(adv)
                return tuple(out_n)
            if not reuse or font is not prim_font or metrics is None:
                return ()
            out: list[float] = []
            for i, ch in enumerate(text):
                em = metrics.advance.get((st.font, ch))
                adv = em * st.size if em is not None else float(font.text_length(ch, fontsize=st.size))
                if i + 1 < len(text):
                    adv += metrics.pair_adjust(st.font, ch, text[i + 1]) * st.size
                out.append(adv)
            return tuple(out)

        def space(font: lay.AnyFont) -> lay.Space:
            em = metrics.gap.get(st.font) if (reuse and metrics) else None
            if em is not None:
                width = em * st.size
            else:
                width = lay.space_for(font, st.size, st.color).width
                if reuse and metrics:
                    width += metrics.spacing.get(st.font, 0.0) * st.size
            # Con la fuente original se dibuja el espacio con su propio código (su
            # ToUnicode es el del PDF). Con otras fuentes no: en las recortadas suele
            # compartir glifo con el espacio duro y saldría «\xa0» al copiar; los
            # lectores deducen el espacio por la separación (como en LaTeX).
            draw = isinstance(font, NativeFont) and " " in font.covered
            return lay.Space(font, st.size, st.color, width, draw)

        for piece, font in picker.assign(st, r.text):
            buf = ""
            for ch in piece + "\0":
                if ch in " \t\u00a0\n\0":
                    if buf:
                        toks.append(lay.Frag(buf, font, st.size, st.color, advances(buf, font)))
                        buf = ""
                    if ch == "\n":
                        toks.append(lay.Break())
                    elif ch != "\0":
                        toks.append(space(font))
                else:
                    buf += ch
    return toks


#: Holguras de partición a probar (pt) para reproducir los cortes del original.
_BREAK_SLACKS = (0.0, 0.25, 0.5, 1.0, 2.0, 3.0, 4.5, 6.0)


def _calibrate_slack(para: Paragraph, tokens: list[lay.Token], x0: float, x1: float) -> float:
    """Holgura mínima con la que el texto original se parte igual que en el PDF."""
    target = [len(ln.text.split()) for ln in para.lines]
    best, best_score = 0.0, 10**9
    for slack in _BREAK_SLACKS:
        if slack > 0.02 * (x1 - x0) + 0.5:
            break
        res = lay.layout(tokens, x0, x1, 0.0, para.line_pitch, para.align, para.indent, slack)
        score = sum(abs(a - b) for a, b in zip(res.line_words, target)) + 10 * abs(
            len(res.line_words) - len(target)
        )
        if score < best_score:
            best, best_score = slack, score
        if score == 0:
            break
    return best


def _native_fonts(
    doc: pymupdf.Document, page_index: int, resolver: FontResolverLike
) -> dict[str, NativeFont]:
    """Fuentes originales utilizables en la página, por nombre de fuente del tramo."""
    embedded_xrefs = getattr(resolver, "embedded_xrefs", None)
    page = doc[page_index]
    if embedded_xrefs is None or doc.xref_get_key(page.xref, "Resources")[0] == "null":
        return {}
    observed: dict[str, set[str]] = {}
    for b in page.get_text("rawdict", flags=_EXTRACT_FLAGS)["blocks"]:
        if b.get("type", 0) != 0:
            continue
        for ln in b["lines"]:
            for sp in ln["spans"]:
                observed.setdefault(sp["font"], set()).update(ch["c"] for ch in sp["chars"])
    out: dict[str, NativeFont] = {}
    for name, chars in observed.items():
        for xref in embedded_xrefs(FontRequest(name, 0, page_index)):
            nf = load_native_font(doc, xref, chars)
            if nf is not None and nf.covered:
                out[name] = nf
                break
    return out


def _check_fits(page: pymupdf.Page, bbox: pymupdf.Rect) -> None:
    if bbox.y1 > page.rect.y1 + 0.5:
        raise TextEditError("El texto no cabe en la página. Acórtalo o reduce el tamaño de letra.")


def _check_inside(page: pymupdf.Page, bbox: pymupdf.Rect) -> None:
    """Al mover un párrafo: que no se salga por ningún lado."""
    r = page.rect
    if bbox.x0 < r.x0 - 0.5 or bbox.y0 < r.y0 - 0.5 or bbox.x1 > r.x1 + 0.5:
        raise TextEditError("El texto se saldría de la página. Muévelo un poco hacia dentro.")
    _check_fits(page, bbox)


def _texts_by_style(runs: Sequence[Run]) -> dict[TextStyle, str]:
    out: dict[TextStyle, str] = {}
    for r in runs:
        out[r.style] = out.get(r.style, "") + r.text
    return out


def _subs_tuple(
    subs: dict[tuple[str, str], set[str]] | set[tuple[str, str]],
) -> tuple[tuple[str, str, str], ...]:
    if isinstance(subs, set):
        return tuple(sorted((clean_font_name(a), b, "") for a, b in subs))
    return tuple(
        sorted((clean_font_name(a), b, "".join(sorted(c))) for (a, b), c in subs.items())
    )


def _other_text_rects(page: pymupdf.Page, own: Sequence[_Line]) -> list[pymupdf.Rect]:
    """Cajas visuales de las palabras de la página que no son del párrafo."""
    m = page.rotation_matrix
    out = []
    for w in page.get_text("words"):
        r = pymupdf.Rect(w[:4]) * m
        c = (r.tl + r.br) / 2
        if not any(ln.bbox.contains(c) for ln in own):
            out.append(r)
    return out


def _overlaps(bbox: pymupdf.Rect, others: Sequence[pymupdf.Rect]) -> bool:
    inner = pymupdf.Rect(bbox.x0 + 1, bbox.y0 + 1, bbox.x1 - 1, bbox.y1 - 1)
    return any(inner.intersects(r) for r in others)


def replace_paragraph(
    doc: pymupdf.Document,
    para: Paragraph,
    runs: Sequence[Run],
    resolver: FontResolverLike,
    align: Align | None = None,
    box: tuple[float, float] | None = None,
    offset: tuple[float, float] = (0.0, 0.0),
) -> EditResult:
    """Sustituye el párrafo `para` por `runs`, refluyendo dentro del ancho original.

    `box` (x0, x1 visuales) sustituye el ancho disponible: el usuario puede
    ensanchar o estrechar la caja. `offset` (dx, dy) mueve el párrafo; con
    `box`, dx ya va incluido en la caja y se ignora. Si el texto nuevo crece,
    la caja se amplía hacia abajo. Si no cabe en la página, lanza
    `TextEditError` sin tocar nada.
    """
    dx, dy = offset
    if para.blocked_reason:
        raise TextEditError(para.blocked_reason)
    page = doc[para.page_index]
    runs = _merge_runs(list(runs))
    x0, y0, x1, _ = para.bbox
    new_text = "".join(r.text for r in runs)
    if not new_text.strip():
        redact_lines(page, para.lines)
        return EditResult((x0, y0, x0, y0), ())
    if lay.needs_shaping(new_text) or lay.needs_shaping(para.text):
        if box is not None:
            dx = box[0] - para.bbox[0]
        return _replace_with_story(doc, para, runs, resolver, align, (dx, dy))

    # Caja de recomposición: la del párrafo, o más ancha si es una línea suelta.
    box_x0, box_x1 = para.box if para.box != (0.0, 0.0) else (x0, x1)
    natives = _native_fonts(doc, para.page_index, resolver)
    picker = _FontPicker(resolver, para.page_index, _texts_by_style(para.runs), natives)
    # La holgura se calibra con la caja original (reproduce los cortes del PDF).
    slack = _calibrate_slack(para, _tokens(para.runs, picker, para.metrics), box_x0, box_x1)
    if box is not None:
        if box[1] - box[0] < 2 * para.size:
            raise TextEditError("La caja es demasiado estrecha para el texto.")
        box_x0, box_x1 = box
        slack = 0.0  # caja nueva: no hay cortes originales que reproducir
    else:
        box_x0, box_x1 = box_x0 + dx, box_x1 + dx
    tokens = _tokens(runs, picker, para.metrics)
    new_align = align or para.align
    if len(para.lines) == 1 and new_align == "justify":
        new_align = "left"  # una línea suelta no se estira
    result = lay.layout(
        tokens, box_x0, box_x1, para.first_baseline + dy, para.line_pitch,
        new_align, para.indent, slack,
    )
    if (dx, dy) != (0.0, 0.0):
        _check_inside(page, result.bbox)
    else:
        _check_fits(page, result.bbox)
    overlaps = _overlaps(result.bbox, _other_text_rects(page, para.lines))
    redact_lines(page, para.lines)
    lay.write(page, result)
    b = result.bbox
    return EditResult((b.x0, b.y0, b.x1, b.y1), _subs_tuple(picker.substitutions), overlaps)


def _replace_with_story(
    doc: pymupdf.Document,
    para: Paragraph,
    runs: Sequence[Run],
    resolver: FontResolverLike,
    align: Align | None,
    offset: tuple[float, float] = (0.0, 0.0),
) -> EditResult:
    """Ruta para escrituras que necesitan motor de forma (árabe, hebreo, índicas...)."""
    page = doc[para.page_index]
    x0, y0, x1, _ = para.bbox
    # Las mismas fuentes para el texto original (calibración) y el nuevo.
    fonts = _resolve_fonts([*para.runs, *runs], resolver, para.page_index)
    new_align = align or para.align
    html_text, css = _build_html(runs, fonts, new_align, para.line_pitch, para.indent)
    ref_html, ref_css = _build_html(para.runs, fonts, para.align, para.line_pitch, para.indent)
    first = para.lines[0].bbox
    target = _Target(
        ref_html, ref_css, para.first_baseline, first.x0, first.x1,
        tuple(ln.bbox.width for ln in para.lines),
    )
    body_x0 = min(ln.bbox.x0 for ln in para.lines[1:]) if para.indent and len(para.lines) > 1 else x0
    rect, bbox = _place(
        page, body_x0, x1, y0, html_text, css, fonts, para.align, target, para.first_baseline
    )
    if offset != (0.0, 0.0):
        rect = rect + (offset[0], offset[1], offset[0], offset[1])
        bbox = bbox + (offset[0], offset[1], offset[0], offset[1])
        _check_inside(page, bbox)
    redact_lines(page, para.lines)
    spare = _write_box(page, _visual_to_raw(page, rect), html_text, css, fonts)
    if spare < 0:  # no debería pasar: ya se midió
        raise TextEditError("No se pudo escribir el texto en la página.")
    subs = {(f.original_name, f.css_family) for f in fonts.values() if f.substituted}
    return EditResult((bbox.x0, bbox.y0, bbox.x1, bbox.y1), _subs_tuple(subs))


def add_text(
    doc: pymupdf.Document,
    page_index: int,
    x: float,
    y: float,
    runs: Sequence[Run],
    resolver: FontResolverLike,
    width: float | None = None,
) -> EditResult:
    """Añade texto nuevo con la esquina superior izquierda en (x, y) visual.

    Con `width`, el texto se ajusta a ese ancho; si no, solo se parte al llegar
    al borde de la página.

    Sin ajuste de línea automático salvo que no quepa en el ancho de la página:
    cada salto de línea del texto es una línea nueva.
    """
    page = doc[page_index]
    runs = _merge_runs(list(runs))
    text = "".join(r.text for r in runs)
    if not text.strip():
        raise TextEditError("El texto está vacío.")
    size = max(r.style.size for r in runs)
    if lay.needs_shaping(text):
        fonts = _resolve_fonts(runs, resolver, page_index)
        html_text, css = _build_html(runs, fonts, "left", 1.2 * size, 0.0)
        css = css.replace("white-space: pre-wrap;", "white-space: pre;")
        ascender = pymupdf.Font(fontbuffer=fonts[runs[0].style].buffer).ascender or 0.8
        rect, bbox = _place(
            page, x, page.rect.x1, y, html_text, css, fonts, "left", None,
            y + ascender * runs[0].style.size,
        )
        if _write_box(page, _visual_to_raw(page, rect), html_text, css, fonts) < 0:
            raise TextEditError("El texto no cabe en la página.")
        subs = {(f.original_name, f.css_family) for f in fonts.values() if f.substituted}
        return EditResult((bbox.x0, bbox.y0, bbox.x1, bbox.y1), _subs_tuple(subs))

    picker = _FontPicker(resolver, page_index, {})
    tokens = _tokens(runs, picker)
    first_font = picker.font(picker.primary(runs[0].style, runs[0].text))
    baseline = y + (first_font.ascender or 0.8) * runs[0].style.size
    right = min(page.rect.x1 - 2, x + width) if width else page.rect.x1 - 2
    result = lay.layout(tokens, x, right, baseline, 1.2 * size, "left")
    _check_fits(page, result.bbox)
    lay.write(page, result)
    b = result.bbox
    return EditResult((b.x0, b.y0, b.x1, b.y1), _subs_tuple(picker.substitutions))


def restyle(runs: Sequence[Run], **changes: object) -> list[Run]:
    """Copia los tramos cambiando atributos de estilo (size, color, font, flags)."""
    return [Run(r.text, replace(r.style, **changes)) for r in runs]  # type: ignore[arg-type]
