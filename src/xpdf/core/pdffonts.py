"""Escritura con las fuentes *originales* del PDF (sin volver a incrustarlas).

Muchas fuentes incrustadas (sobre todo las de Word, Identity-H) son recortes sin
tabla de caracteres utilizable: no se pueden usar como fuente «normal», pero el
propio PDF ya sabe dibujar con ellas. Aquí se lee el objeto de fuente:

- su `ToUnicode` invertido da, para cada carácter, el código que lo dibuja;
- `/W` (Type0) o `/Widths` (simples) dan los avances exactos;
- `/Ascent` y `/Descent` del descriptor, las alturas.

Con eso se escribe texto nuevo en el flujo de contenido usando el mismo recurso
de fuente que el original: idéntico aspecto y métricas, y el archivo no crece.

Solo se consideran disponibles los caracteres que el documento ya dibuja con esa
fuente (seguro: su glifo existe) y las entradas sueltas `bfchar` del ToUnicode,
que los generadores escriben solo para los glifos incluidos en el recorte.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import pymupdf

log = logging.getLogger(__name__)

_TOKEN = re.compile(r"\[|\]|-?\d+(?:\.\d+)?")


@dataclass(slots=True)
class NativeFont:
    """Fuente original del PDF, utilizable para escribir y medir."""

    xref: int
    name: str  # nombre limpio, para mostrar
    two_bytes: bool  # Identity-H: códigos de 2 bytes; fuentes simples: 1 byte
    codes: dict[str, int]  # carácter -> código
    widths: dict[int, float]  # código -> avance en em
    default_width: float
    ascender: float
    descender: float
    covered: frozenset[str] = field(default_factory=frozenset)

    # --- interfaz compatible con pymupdf.Font en lo que usa el compositor ---
    def has_glyph(self, cp: int) -> bool:
        return chr(cp) in self.covered

    def glyph_advance(self, cp: int) -> float:
        code = self.codes.get(chr(cp))
        return self.widths.get(code, self.default_width) if code is not None else self.default_width

    def text_length(self, text: str, fontsize: float = 11) -> float:
        return sum(self.glyph_advance(ord(c)) for c in text) * fontsize

    def encode(self, text: str) -> bytes:
        out = bytearray()
        for c in text:
            code = self.codes[c]
            out += code.to_bytes(2 if self.two_bytes else 1, "big")
        return bytes(out)


# ------------------------------------------------------------------ lectura
def _resolve(doc: pymupdf.Document, value: tuple[str, str]) -> str:
    kind, text = value
    if kind == "xref":
        return str(doc.xref_object(int(text.split()[0]), compressed=True))
    return text


def _number(doc: pymupdf.Document, xref: int, key: str) -> float | None:
    kind, text = doc.xref_get_key(xref, key)
    if kind == "null":
        return None
    try:
        return float(_resolve(doc, (kind, text)))
    except ValueError:
        return None


def _parse_cid_widths(text: str) -> dict[int, float]:
    """/W de una fuente CID: `c [w1 w2 ...]` o `c_first c_last w`."""
    tokens = _TOKEN.findall(text)
    out: dict[int, float] = {}
    i = 0
    # se salta el corchete exterior
    if tokens and tokens[0] == "[":
        tokens = tokens[1:-1] if tokens[-1] == "]" else tokens[1:]
    while i < len(tokens):
        first = int(float(tokens[i]))
        if i + 1 < len(tokens) and tokens[i + 1] == "[":
            j = i + 2
            c = first
            while j < len(tokens) and tokens[j] != "]":
                out[c] = float(tokens[j]) / 1000
                c += 1
                j += 1
            i = j + 1
        elif i + 2 < len(tokens):
            last = int(float(tokens[i + 1]))
            w = float(tokens[i + 2]) / 1000
            for c in range(first, last + 1):
                out[c] = w
            i += 3
        else:
            break
    return out


def _hex_to_str(h: str) -> str:
    data = bytes.fromhex(h)
    if len(data) % 2:
        data = b"\x00" + data
    return data.decode("utf-16-be", errors="replace")


def parse_tounicode(cmap: str) -> tuple[dict[int, str], set[int]]:
    """(código -> texto, códigos declarados uno a uno con bfchar)."""
    mapping: dict[int, str] = {}
    singles: set[int] = set()
    for block in re.findall(r"beginbfchar(.*?)endbfchar", cmap, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            code = int(src, 16)
            mapping[code] = _hex_to_str(dst)
            singles.add(code)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", cmap, re.S):
        for lo, hi, rest in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", block
        ):
            a, b = int(lo, 16), int(hi, 16)
            if b - a > 0xFFFF:
                continue
            if rest.startswith("["):
                for k, h in enumerate(re.findall(r"<([0-9A-Fa-f]+)>", rest)):
                    mapping[a + k] = _hex_to_str(h)
            else:
                base = _hex_to_str(rest[1:-1])
                if not base:
                    continue
                for k in range(b - a + 1):
                    mapping[a + k] = base[:-1] + chr(ord(base[-1]) + k)
    return mapping, singles


def load_native_font(
    doc: pymupdf.Document, xref: int, observed: Iterable[str] = ()
) -> NativeFont | None:
    """Lee la fuente `xref` del PDF. None si no se puede escribir con ella sin riesgo."""
    try:
        return _load(doc, xref, set(observed))
    except Exception:
        log.debug("Fuente nativa %d no utilizable", xref, exc_info=True)
        return None


def _load(doc: pymupdf.Document, xref: int, observed: set[str]) -> NativeFont | None:
    subtype = doc.xref_get_key(xref, "Subtype")[1]
    name = doc.xref_get_key(xref, "BaseFont")[1].lstrip("/")
    if "+" in name[:7]:
        name = name.split("+", 1)[1]
    tu_kind, tu_val = doc.xref_get_key(xref, "ToUnicode")
    to_unicode: dict[int, str] = {}
    singles: set[int] = set()
    if tu_kind == "xref":
        stream = doc.xref_stream(int(tu_val.split()[0]))
        if stream:
            to_unicode, singles = parse_tounicode(stream.decode("latin-1"))

    if subtype == "/Type0":
        if doc.xref_get_key(xref, "Encoding")[1] != "/Identity-H" or not to_unicode:
            return None
        desc_kind, desc_val = doc.xref_get_key(xref, "DescendantFonts")
        desc_text = _resolve(doc, (desc_kind, desc_val))
        m = re.search(r"(\d+)\s+0\s+R", desc_text)
        if not m:
            return None
        dx = int(m.group(1))
        w_kind, w_val = doc.xref_get_key(dx, "W")
        widths = _parse_cid_widths(_resolve(doc, (w_kind, w_val))) if w_kind != "null" else {}
        dw = (_number(doc, dx, "DW") or 1000) / 1000
        fd = doc.xref_get_key(dx, "FontDescriptor")
        two = True
    elif subtype in ("/TrueType", "/Type1"):
        enc = doc.xref_get_key(xref, "Encoding")
        first = _number(doc, xref, "FirstChar")
        w_kind, w_val = doc.xref_get_key(xref, "Widths")
        if first is None or w_kind == "null":
            return None
        nums = [float(t) for t in _TOKEN.findall(_resolve(doc, (w_kind, w_val))) if t not in "[]"]
        widths = {int(first) + i: w / 1000 for i, w in enumerate(nums)}
        dw = 0.0
        fd = doc.xref_get_key(xref, "FontDescriptor")
        two = False
        if not to_unicode:
            if enc[1] not in ("/WinAnsiEncoding",):
                return None  # codificación propia sin ToUnicode: no se sabe qué código es cada letra
            for code in widths:
                try:
                    to_unicode[code] = bytes([code]).decode("cp1252")
                except UnicodeDecodeError:
                    continue
    else:
        return None  # Type3 y demás: no

    if fd[0] != "xref":
        return None
    fdx = int(fd[1].split()[0])
    has_file = any(doc.xref_get_key(fdx, k)[0] != "null" for k in ("FontFile", "FontFile2", "FontFile3"))
    if not has_file:
        return None  # no incrustada: el visor usaría otra fuente
    asc = (_number(doc, fdx, "Ascent") or 800) / 1000
    desc = (_number(doc, fdx, "Descent") or -200) / 1000

    codes: dict[str, int] = {}
    for code, text in sorted(to_unicode.items()):
        if len(text) == 1 and text not in codes:
            codes[text] = code
    # Disponibles: lo que ya se dibuja con ella y las entradas sueltas del ToUnicode.
    covered = {c for c in observed if c in codes}
    covered |= {to_unicode[c] for c in singles if len(to_unicode.get(c, "")) == 1}
    if " " in codes:
        covered.add(" ")
    return NativeFont(
        xref=xref,
        name=name,
        two_bytes=two,
        codes=codes,
        widths=widths,
        default_width=dw,
        ascender=asc,
        descender=desc if desc < 0 else -desc,
        covered=frozenset(covered),
    )


class InsertedFont:
    """Fuente de respaldo registrada en la página con `insert_font` (Identity-H).

    Se escribe con el número de glifo como código; PyMuPDF genera su ToUnicode.
    """

    def __init__(self, page: pymupdf.Page, font: pymupdf.Font, resource: str) -> None:
        self.font = font
        self.resource = resource
        self.xref = int(page.insert_font(fontname=resource, fontbuffer=font.buffer))
        self.two_bytes = True

    def glyph_advance(self, cp: int) -> float:
        return float(self.font.glyph_advance(cp))

    def encode(self, text: str) -> bytes:
        return b"".join(int(self.font.has_glyph(ord(c))).to_bytes(2, "big") for c in text)


# ---------------------------------------------------------------- escritura
@dataclass(frozen=True, slots=True)
class NativeRun:
    """Fragmento a escribir, en coordenadas visuales.

    `font` es la fuente original del PDF (`NativeFont`) o una de respaldo ya
    registrada en la página (`InsertedFont`).
    """

    x: float
    baseline: float
    text: str
    font: NativeFont | InsertedFont
    size: float
    color: int
    advances: tuple[float, ...] = ()  # avance real (pt) de cada carácter


def _resource_name(page: pymupdf.Page, xref: int) -> str | None:
    """Nombre del recurso de fuente en la página (lo añade si hace falta)."""
    doc = page.parent
    name = f"XPF{xref}"
    kind, val = doc.xref_get_key(page.xref, "Resources")
    if kind == "null":
        return None  # recursos heredados: no se tocan (se usa la ruta normal)
    if kind == "xref":
        res_xref = int(val.split()[0])
        fkind, fval = doc.xref_get_key(res_xref, "Font")
        if fkind == "xref":
            doc.xref_set_key(int(fval.split()[0]), name, f"{xref} 0 R")
        else:
            doc.xref_set_key(res_xref, f"Font/{name}", f"{xref} 0 R")
        return name
    fkind, fval = doc.xref_get_key(page.xref, "Resources/Font")
    if fkind == "xref":
        doc.xref_set_key(int(fval.split()[0]), name, f"{xref} 0 R")
    else:
        doc.xref_set_key(page.xref, f"Resources/Font/{name}", f"{xref} 0 R")
    return name


def write_native(page: pymupdf.Page, runs: Sequence[NativeRun]) -> bool:
    """Añade los fragmentos al contenido de la página. False si no se pudo."""
    if not runs:
        return True
    names: dict[int, str] = {}
    for r in runs:
        if r.font.xref not in names:
            if isinstance(r.font, InsertedFont):
                names[r.font.xref] = r.font.resource
                continue
            n = _resource_name(page, r.font.xref)
            if n is None:
                return False
            names[r.font.xref] = n
    # visual -> espacio PDF (y hacia arriba), incluida la rotación de la página
    to_pdf = ~page.rotation_matrix * ~page.transformation_matrix
    lin = pymupdf.Matrix(to_pdf.a, to_pdf.b, to_pdf.c, to_pdf.d, 0, 0)
    u = pymupdf.Point(1, 0) * lin  # dirección de la línea base
    v = pymupdf.Point(0, -1) * lin  # «hacia arriba» visual
    ops: list[str] = ["q"]
    for r in runs:
        o = pymupdf.Point(r.x, r.baseline) * to_pdf
        rgb = ((r.color >> 16) & 255) / 255, ((r.color >> 8) & 255) / 255, (r.color & 255) / 255
        ops.append(f"BT /{names[r.font.xref]} {r.size:.4f} Tf {rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f} rg")
        ops.append(f"{u.x:.6f} {u.y:.6f} {v.x:.6f} {v.y:.6f} {o.x:.4f} {o.y:.4f} Tm")
        # TJ con ajustes para que cada carácter caiga en su avance exacto.
        parts: list[str] = []
        for i, ch in enumerate(r.text):
            parts.append(f"<{r.font.encode(ch).hex()}>")
            if r.advances and i < len(r.advances) - 1:
                natural = r.font.glyph_advance(ord(ch)) * r.size
                adj = (natural - r.advances[i]) / r.size * 1000
                if abs(adj) > 0.01:
                    parts.append(f"{adj:.3f}")
        ops.append("[" + " ".join(parts) + "] TJ ET")
    ops.append("Q")
    data = ("\n".join(ops) + "\n").encode("latin-1")
    doc = page.parent
    page.wrap_contents()  # el contenido previo no debe alterar nuestro estado gráfico
    new = doc.get_new_xref()
    doc.update_object(new, "<<>>")
    doc.update_stream(new, data)
    contents = [*page.get_contents(), new]
    doc.xref_set_key(page.xref, "Contents", "[" + " ".join(f"{x} 0 R" for x in contents) + "]")
    return True
