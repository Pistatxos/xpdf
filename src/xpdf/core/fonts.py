"""Elección de la fuente con la que reescribir texto editado.

Orden de preferencia para cada tramo (span) de texto:

1. **La fuente incrustada en el PDF**, si contiene *todos* los glifos del texto
   nuevo. Las fuentes incrustadas suelen ser subconjuntos (``ABCDEF+Nombre``) y
   muchas no tienen una tabla ``cmap`` Unicode utilizable (CID con Identity-H
   tras subsetting, Type1C con nombres ``g12``…); se verifica con el mismo
   mecanismo que usa el motor HTML de MuPDF (``fz_encode_character``) más una
   comprobación de que el glifo tiene contorno. Si no se puede verificar, la
   fuente se considera inutilizable. Las Type3 nunca se reutilizan.
2. **La misma fuente instalada en el sistema**, buscada por nombre PostScript,
   nombre completo o familia + negrita/cursiva (con alias métricamente
   compatibles: Helvetica→Arial→Liberation Sans, etc.). El índice de fuentes del
   sistema se construye perezosamente y se cachea en JSON.
3. **La fuente incluida más parecida** (Liberation Sans/Serif/Mono), elegida por
   clasificación serif / sans / mono, negrita y cursiva.

Si ni siquiera la fuente incluida cubre el texto (p. ej. CJK, árabe, emoji) se
devuelve igualmente: ``page.insert_htmlbox`` recurre automáticamente, carácter a
carácter, a las fuentes de reserva integradas en MuPDF (Droid Sans Fallback para
CJK, Noto Naskh Arabic, Noto Serif Devanagari, Noto Emoji, Noto Sans Symbols…).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import struct
import sys
import threading
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Literal

import pymupdf

from xpdf import paths

log = logging.getLogger(__name__)

# Bits de span["flags"] de get_text("dict").
FLAG_ITALIC = 2
FLAG_SERIF = 4
FLAG_MONO = 8
FLAG_BOLD = 16

FontSource = Literal["embedded", "system", "bundled"]
FontClass = Literal["sans", "serif", "mono"]

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
_FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc"}
_INDEX_VERSION = 1


# ============================================================== API pública
@dataclass(frozen=True, slots=True)
class FontRequest:
    """Fuente original de un tramo de texto, tal como la reporta PyMuPDF."""

    name: str  # span["font"], p. ej. "ABCDEF+Calibri-Bold"
    flags: int  # span["flags"]
    page_index: int


@dataclass(frozen=True, slots=True)
class ResolvedFont:
    """Fuente elegida para escribir, lista para un ``pymupdf.Archive``."""

    css_family: str  # identificador seguro para CSS font-family
    buffer: bytes  # bytes del archivo de fuente
    filename: str  # nombre dentro del Archive / src de @font-face
    source: FontSource
    original_name: str  # nombre original sin prefijo de subconjunto
    bold: bool  # la cara elegida ya es negrita (no sintetizar en CSS)
    italic: bool

    @property
    def substituted(self) -> bool:
        return self.source != "embedded"


@dataclass(frozen=True, slots=True)
class FontStyle:
    """Clasificación de la fuente original."""

    cls: FontClass
    bold: bool
    italic: bool


# ========================================================= clasificación
def clean_font_name(name: str) -> str:
    """Quita el prefijo de subconjunto ``ABCDEF+`` y espacios sobrantes."""
    return _SUBSET_PREFIX.sub("", name.strip()).strip()


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


_MONO_HINTS = ("courier", "mono", "consol", "menlo", "monaco", "typewriter", "code", "fixed")
_SANS_HINTS = (
    "sans", "arial", "helvetica", "calibri", "verdana", "tahoma", "segoe", "gill",
    "futura", "roboto", "lato", "opensans", "frutiger", "univers", "myriad", "trebuchet",
    "candara", "corbel", "aptos", "avenir", "geneva", "lucidagrande", "franklin",
    "century gothic", "centurygothic", "carlito", "arimo", "dejavusans",
    "ubuntu", "source sans", "sourcesans", "gothic", "grotesk", "nimbussans",
)  # fmt: skip
_SERIF_HINTS = (
    "serif", "times", "georgia", "garamond", "cambria", "antiqua", "palatino", "book",
    "minion", "baskerville", "century", "roman", "caslon", "bodoni", "didot", "caladea",
    "tinos", "constantia", "charter", "utopia", "newyork", "nimbusroman", "mincho", "song",
)  # fmt: skip
_BOLD_HINTS = ("bold", "black", "heavy", "semibold", "demibold", "demi", "extrabold")
_ITALIC_HINTS = ("italic", "oblique", "slanted", "kursiv")


def classify(name: str, flags: int) -> FontStyle:
    """Clasifica la fuente combinando ``flags`` (poco fiables) y su nombre."""
    clean = clean_font_name(name)
    low = clean.lower()
    n = _norm(clean)
    # Parte de estilo: lo que va tras '-' o ',' (p. ej. "Arial,BoldItalic", "Foo-BoldIt").
    style_part = _norm(re.split(r"[-,]", clean, maxsplit=1)[1]) if re.search(r"[-,]", clean) else ""

    bold = bool(flags & FLAG_BOLD) or any(h in n for h in _BOLD_HINTS)
    italic = (
        bool(flags & FLAG_ITALIC)
        or any(h in n for h in _ITALIC_HINTS)
        or style_part in ("it", "boldit", "blackit", "semiboldit", "lightit", "mediumit")
        or style_part.endswith("it")
    )

    cls: FontClass
    if bool(flags & FLAG_MONO) or any(h in n for h in _MONO_HINTS):
        cls = "mono"
    elif any(h.replace(" ", "") in n for h in _SANS_HINTS) or "sans" in low:
        cls = "sans"
    elif any(h in n for h in _SERIF_HINTS):
        cls = "serif"
    elif flags & FLAG_SERIF:
        cls = "serif"
    else:
        cls = "sans"
    return FontStyle(cls, bold, italic)


_STYLE_WORDS = {
    "regular", "roman", "normal", "book", "bold", "italic", "oblique", "bolditalic",
    "boldoblique", "it", "boldit", "semibold", "demibold", "light", "medium", "black",
    "heavy", "mt", "ps", "psmt",
}  # fmt: skip

# Alias: familia original normalizada -> familias candidatas (en orden).
_FAMILY_ALIASES: dict[str, tuple[str, ...]] = {
    "helvetica": ("helvetica", "arial", "liberationsans", "arimo"),
    "arial": ("arial", "helvetica", "liberationsans", "arimo"),
    "times": ("times", "timesnewroman", "liberationserif", "tinos"),
    "timesroman": ("times", "timesnewroman", "liberationserif", "tinos"),
    "timesnewroman": ("timesnewroman", "times", "liberationserif", "tinos"),
    "courier": ("courier", "couriernew", "liberationmono", "cousine"),
    "couriernew": ("couriernew", "courier", "liberationmono", "cousine"),
    "calibri": ("calibri", "carlito"),
    "cambria": ("cambria", "caladea"),
}


def _family_candidates(clean: str) -> list[str]:
    """Familias normalizadas candidatas para un nombre de fuente limpio."""
    base = re.split(r"[-,]", clean, maxsplit=1)[0]
    words = base.split()
    while len(words) > 1 and _norm(words[-1]) in _STYLE_WORDS:
        words.pop()
    fam = _norm(" ".join(words))
    for suffix in ("psmt", "mt", "ps"):
        if fam.endswith(suffix) and len(fam) - len(suffix) >= 3:
            fam = fam[: -len(suffix)]
            break
    # "TimesNewRomanPS" -> "timesnewroman"; "Times-Roman" -> "times".
    out = [fam]
    for alias in _FAMILY_ALIASES.get(fam, ()):
        if alias not in out:
            out.append(alias)
    return out


# ============================================================ cobertura
def _ignorable(ch: str) -> bool:
    return ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Zs", "Zl", "Zp", "Co", "Cn")


def needed_chars(text: str) -> frozenset[str]:
    """Caracteres que la fuente debe poder dibujar (sin espacios ni controles)."""
    return frozenset(ch for ch in text if not _ignorable(ch))


class _GlyphChecker:
    """Comprueba glifos de un buffer de fuente igual que lo hará insert_htmlbox."""

    def __init__(self, buffer: bytes) -> None:
        self.font: pymupdf.Font | None
        try:
            self.font = pymupdf.Font(fontbuffer=buffer)
        except Exception:  # buffer dañado o formato no admitido
            log.debug("No se pudo cargar la fuente (%d bytes)", len(buffer))
            self.font = None
        # En sfnt (TrueType/OpenType) exigimos una cmap Unicode real: sin ella
        # FreeType usa la cmap Mac/símbolo y el glifo devuelto puede ser otro.
        self.usable = self.font is not None and _sfnt_unicode_ok(buffer)
        self._cache: dict[str, bool] = {}

    def has(self, ch: str) -> bool:
        hit = self._cache.get(ch)
        if hit is None:
            hit = self._check(ch)
            self._cache[ch] = hit
        return hit

    def _check(self, ch: str) -> bool:
        if not self.usable or self.font is None:
            return False
        gid = int(self.font.has_glyph(ord(ch)))
        if gid <= 0:
            return False
        # Subconjuntos que conservan la cmap completa pero vacían los contornos.
        try:
            mupdf = pymupdf.mupdf
            path = mupdf.fz_outline_glyph(self.font.this, gid, mupdf.FzMatrix())
            if not path.m_internal:
                return True  # p. ej. bitmap: no se puede comprobar, se acepta
            r = mupdf.fz_bound_path(path, mupdf.FzStrokeState(), mupdf.FzMatrix())
            return bool(r.x0 < r.x1 and r.y0 < r.y1)
        except Exception:
            return True


def _sfnt_unicode_ok(buffer: bytes) -> bool:
    """True si no es sfnt, o si es sfnt con una subtabla cmap Unicode."""
    tag = buffer[:4]
    if tag not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"):
        return True  # CFF desnuda / Type1: FreeType sintetiza la cmap por nombres
    try:
        tables = _sfnt_tables(buffer, 0)
        cmap = tables.get("cmap")
        if cmap is None:
            return False
        off, _length = cmap
        _ver, n = struct.unpack_from(">HH", buffer, off)
        for i in range(n):
            plat, enc, _sub = struct.unpack_from(">HHI", buffer, off + 4 + 8 * i)
            if plat == 0 or (plat == 3 and enc in (1, 10)):
                return True
        return False
    except (struct.error, ValueError):
        return False


# ============================================================ sfnt / TTC
def _sfnt_tables(data: bytes, base: int) -> dict[str, tuple[int, int]]:
    """Tablas de la fuente sfnt que empieza en ``base``: tag -> (offset, length)."""
    if data[base : base + 4] == b"ttcf":
        base = struct.unpack_from(">I", data, 12)[0]
    num = struct.unpack_from(">H", data, base + 4)[0]
    out: dict[str, tuple[int, int]] = {}
    for i in range(num):
        tag, _cs, off, length = struct.unpack_from(">4sIII", data, base + 12 + 16 * i)
        out[tag.decode("latin-1")] = (off, length)
    return out


def extract_ttc_face(data: bytes, index: int) -> bytes:
    """Extrae la cara ``index`` de una colección TTC/OTC como fuente sfnt aislada.

    El motor HTML de MuPDF carga siempre la cara 0 de un buffer, así que para
    usar otra cara (p. ej. Helvetica-Bold dentro de Helvetica.ttc) hay que
    reconstruir un archivo independiente con sus tablas.
    """
    if data[:4] != b"ttcf":
        if index != 0:
            raise ValueError("no es una colección TTC")
        return data
    num_fonts = struct.unpack_from(">I", data, 8)[0]
    if not 0 <= index < num_fonts:
        raise ValueError(f"índice de cara fuera de rango: {index}")
    base = struct.unpack_from(">I", data, 12 + 4 * index)[0]
    version, num = struct.unpack_from(">IH", data, base)
    records = [struct.unpack_from(">4sIII", data, base + 12 + 16 * i) for i in range(num)]
    records.sort(key=lambda r: r[0])
    header_len = 12 + 16 * num
    search = 1
    sel = 0
    while search * 2 <= num:
        search *= 2
        sel += 1
    out = bytearray(struct.pack(">IHHHH", version, num, search * 16, sel, num * 16 - search * 16))
    body = bytearray()
    offset = header_len
    for tag, checksum, off, length in records:
        out += struct.pack(">4sIII", tag, checksum, offset + len(body), length)
        body += data[off : off + length]
        body += b"\x00" * (-len(body) % 4)
    return bytes(out + body)


def _read_name_table(f: BinaryIO, off: int, length: int) -> dict[int, str]:
    f.seek(off)
    raw = f.read(length)
    _fmt, count, str_off = struct.unpack_from(">HHH", raw, 0)
    best: dict[int, tuple[int, str]] = {}
    for i in range(count):
        plat, enc, lang, nid, ln, so = struct.unpack_from(">HHHHHH", raw, 6 + 12 * i)
        if nid not in (1, 2, 4, 6, 16, 17):
            continue
        chunk = raw[str_off + so : str_off + so + ln]
        if plat == 3 or plat == 0:
            prio = 0 if (plat == 3 and lang == 0x409) else 1
            text = chunk.decode("utf-16-be", errors="replace")
        elif plat == 1 and enc == 0:
            prio = 2 if lang == 0 else 3
            text = chunk.decode("mac_roman", errors="replace")
        else:
            continue
        text = text.strip("\x00 ")
        if text and (nid not in best or prio < best[nid][0]):
            best[nid] = (prio, text)
    return {k: v[1] for k, v in best.items()}


@dataclass(slots=True)
class SystemFace:
    """Una cara de fuente instalada en el sistema."""

    path: str
    index: int  # cara dentro de una TTC (0 si no es colección)
    postscript: str
    family: str  # familia tipográfica (nameID 16, o 1)
    full: str
    bold: bool
    italic: bool
    mono: bool
    embeddable: bool  # OS/2 fsType no prohíbe incrustar
    style: str = ""  # subfamilia (nameID 17, o 2)


def _parse_face(f: BinaryIO, path: str, index: int, base: int) -> SystemFace | None:
    f.seek(base)
    head = f.read(12)
    if len(head) < 12:
        return None
    num = struct.unpack_from(">H", head, 4)[0]
    recs = f.read(16 * num)
    tables: dict[bytes, tuple[int, int]] = {}
    for i in range(num):
        tag, _cs, off, length = struct.unpack_from(">4sIII", recs, 16 * i)
        tables[tag] = (off, length)
    if b"name" not in tables:
        return None
    names = _read_name_table(f, *tables[b"name"])
    bold = italic = mono = False
    embeddable = True
    if b"OS/2" in tables:
        f.seek(tables[b"OS/2"][0])
        os2 = f.read(64)
        if len(os2) >= 64:
            weight, fstype = struct.unpack_from(">H", os2, 4)[0], struct.unpack_from(">H", os2, 8)[0]
            fs_sel = struct.unpack_from(">H", os2, 62)[0]
            bold = bool(fs_sel & 0x20) or weight >= 600
            italic = bool(fs_sel & 0x01)
            # 0x0002 = "Restricted License embedding": no se puede incrustar.
            embeddable = (fstype & 0x000F) != 0x0002
    if b"head" in tables:
        f.seek(tables[b"head"][0] + 44)
        mac_style = struct.unpack(">H", f.read(2))[0]
        bold = bold or bool(mac_style & 1)
        italic = italic or bool(mac_style & 2)
    if b"post" in tables:
        f.seek(tables[b"post"][0] + 12)
        mono = struct.unpack(">I", f.read(4))[0] != 0
    family = names.get(16) or names.get(1) or ""
    if not family:
        return None
    return SystemFace(
        path=path,
        index=index,
        postscript=names.get(6, ""),
        family=family,
        full=names.get(4, ""),
        bold=bold,
        italic=italic,
        mono=mono,
        embeddable=embeddable,
        style=names.get(17) or names.get(2) or "",
    )


def read_font_file(path: str) -> list[SystemFace]:
    """Lee solo las cabeceras necesarias de un .ttf/.otf/.ttc (rápido)."""
    faces: list[SystemFace] = []
    try:
        with open(path, "rb") as f:
            tag = f.read(4)
            if tag == b"ttcf":
                f.seek(8)
                n = struct.unpack(">I", f.read(4))[0]
                offsets = struct.unpack(f">{n}I", f.read(4 * n))
                for i, off in enumerate(offsets):
                    face = _parse_face(f, path, i, off)
                    if face:
                        faces.append(face)
            elif tag in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                face = _parse_face(f, path, 0, 0)
                if face:
                    faces.append(face)
    except (OSError, struct.error, ValueError):
        log.debug("Fuente ilegible: %s", path)
    return faces


# ======================================================= índice del sistema
def system_font_dirs() -> list[Path]:
    """Carpetas estándar de fuentes según el SO."""
    home = Path.home()
    if sys.platform == "darwin":
        return [
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            home / "Library" / "Fonts",
        ]
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        dirs = [Path(windir) / "Fonts"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
        return dirs
    return [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        home / ".local" / "share" / "fonts",
        home / ".fonts",
    ]


@dataclass(slots=True)
class _Index:
    dirs: dict[str, float] = field(default_factory=dict)  # carpeta -> mtime
    files: dict[str, dict[str, Any]] = field(default_factory=dict)  # ruta -> {mtime,size,faces}


class SystemFontIndex:
    """Índice perezoso y cacheado (JSON) de las fuentes instaladas.

    La caché se invalida si cambia el mtime de alguna carpeta escaneada (alta o
    baja de archivos) o el conjunto de carpetas raíz; al reescanear se reutilizan
    las entradas de los archivos cuyo mtime y tamaño no cambiaron.
    """

    def __init__(self, dirs: Iterable[Path] | None = None, cache_file: Path | None = None) -> None:
        self._roots = [Path(d) for d in (dirs if dirs is not None else system_font_dirs())]
        self._cache_file = cache_file
        self._faces: list[SystemFace] | None = None
        self._lock = threading.Lock()
        self.scanned_files = 0  # archivos analizados en la última construcción (tests)

    @property
    def cache_file(self) -> Path:
        if self._cache_file is None:
            self._cache_file = paths.user_data_dir() / "font-index.json"
        return self._cache_file

    def faces(self) -> list[SystemFace]:
        with self._lock:
            if self._faces is None:
                self._faces = self._build()
            return self._faces

    # ---------------------------------------------------------------- interno
    def _roots_key(self) -> list[str]:
        return sorted(str(r) for r in self._roots)

    def _load(self) -> tuple[_Index, list[str]] | None:
        try:
            raw = json.loads(self.cache_file.read_text("utf-8"))
            if raw.get("version") != _INDEX_VERSION:
                return None
            return _Index(dirs=raw["dirs"], files=raw["files"]), list(raw["roots"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _dirs_unchanged(idx: _Index) -> bool:
        for d, mtime in idx.dirs.items():
            try:
                if os.stat(d).st_mtime != mtime:
                    return False
            except OSError:
                return False
        return True

    def _walk(self) -> Iterator[tuple[str, str | None]]:
        """Produce (carpeta, None) y (carpeta, archivo) para cada fuente."""
        for root in self._roots:
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                yield dirpath, None
                for fn in sorted(filenames):
                    if os.path.splitext(fn)[1].lower() in _FONT_EXTENSIONS:
                        yield dirpath, os.path.join(dirpath, fn)

    def _build(self) -> list[SystemFace]:
        cached = self._load()
        if cached is not None:
            idx, roots = cached
            if roots == self._roots_key() and self._dirs_unchanged(idx):
                self.scanned_files = 0
                return self._faces_from(idx)
        old_files = cached[0].files if cached else {}
        idx = _Index()
        scanned = 0
        for dirpath, fpath in self._walk():
            if fpath is None:
                try:
                    idx.dirs[dirpath] = os.stat(dirpath).st_mtime
                except OSError:
                    pass
                continue
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            prev = old_files.get(fpath)
            if prev and prev.get("mtime") == st.st_mtime and prev.get("size") == st.st_size:
                idx.files[fpath] = prev
                continue
            scanned += 1
            faces = read_font_file(fpath)
            idx.files[fpath] = {
                "mtime": st.st_mtime,
                "size": st.st_size,
                "faces": [asdict(fc) for fc in faces],
            }
        self.scanned_files = scanned
        self._save(idx)
        log.info("Índice de fuentes del sistema: %d archivos (%d analizados)", len(idx.files), scanned)
        return self._faces_from(idx)

    def _save(self, idx: _Index) -> None:
        data = {
            "version": _INDEX_VERSION,
            "roots": self._roots_key(),
            "dirs": idx.dirs,
            "files": idx.files,
        }
        try:
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), "utf-8")
            os.replace(tmp, self.cache_file)
        except OSError:
            log.warning("No se pudo guardar la caché de fuentes en %s", self.cache_file)

    @staticmethod
    def _faces_from(idx: _Index) -> list[SystemFace]:
        out: list[SystemFace] = []
        for entry in idx.files.values():
            for fc in entry.get("faces", []):
                try:
                    out.append(SystemFace(**fc))
                except TypeError:
                    continue
        return out


_default_index: SystemFontIndex | None = None
_default_lock = threading.Lock()


def default_system_index() -> SystemFontIndex:
    """Índice compartido por toda la aplicación (se construye al primer uso)."""
    global _default_index
    with _default_lock:
        if _default_index is None:
            _default_index = SystemFontIndex()
        return _default_index


# ================================================================ incluidas
_BUNDLED_FAMILY: dict[FontClass, str] = {
    "sans": "LiberationSans",
    "serif": "LiberationSerif",
    "mono": "LiberationMono",
}


def bundled_font_dir() -> Path:
    return paths.assets_dir() / "fonts"


def bundled_font_file(style: FontStyle, directory: Path | None = None) -> Path:
    suffix = {
        (False, False): "Regular",
        (True, False): "Bold",
        (False, True): "Italic",
        (True, True): "BoldItalic",
    }[(style.bold, style.italic)]
    return (directory or bundled_font_dir()) / f"{_BUNDLED_FAMILY[style.cls]}-{suffix}.ttf"


# ================================================================ resolver
def _ext_of(buffer: bytes, fallback: str = "ttf") -> str:
    tag = buffer[:4]
    if tag == b"OTTO":
        return "otf"
    if tag in (b"\x00\x01\x00\x00", b"true"):
        return "ttf"
    return fallback


#: Restos de nombre que no cambian el estilo («Arial» ~ «ArialMT» ~ «Arial Regular»).
_NEUTRAL_SUFFIXES = {"", "regular", "roman", "book", "normal", "mt", "psmt", "ps", "std", "pro"}


class FontResolver:
    """Elige la fuente para reescribir cada tramo de texto de un documento."""

    def __init__(
        self,
        doc: pymupdf.Document,
        *,
        system_index: SystemFontIndex | None = None,
        bundled_dir: Path | None = None,
    ) -> None:
        self._doc = doc
        self._system_index = system_index
        self._bundled_dir = bundled_dir
        self._results: dict[tuple[str, int, int, frozenset[str]], ResolvedFont] = {}
        self._checkers: dict[bytes, _GlyphChecker] = {}  # sha1 -> comprobador
        self._embedded: dict[int, tuple[str, bytes] | None] = {}  # xref -> (ext, buffer)
        self._page_fonts: dict[int, list[tuple[int, str, str]]] = {}
        self._files: dict[tuple[str, int], bytes] = {}

    # ------------------------------------------------------------------ API
    def resolve(self, req: FontRequest, text: str) -> ResolvedFont:
        chars = needed_chars(text)
        key = (req.name, req.flags, req.page_index, chars)
        hit = self._results.get(key)
        if hit is not None:
            return hit
        clean = clean_font_name(req.name)
        style = classify(req.name, req.flags)
        # Si la fuente está incrustada, su archivo dice la verdad sobre negrita y
        # cursiva (las banderas de MuPDF fallan a menudo). Vale también para elegir
        # sustituta cuando la incrustada no cubre el texto.
        actual = self._embedded_style(req, clean)
        if actual is not None:
            style = FontStyle(style.cls, actual[0], actual[1])
        result = (
            self._try_embedded(req, clean, style, chars)
            or self._try_system(clean, style, chars)
            or self._bundled(clean, style, chars)
        )
        log.debug("Fuente para %r (%s): %s %s", req.name, style, result.source, result.filename)
        self._results[key] = result
        return result

    def missing_glyphs(self, buffer: bytes, text: str) -> set[str]:
        """Caracteres de ``text`` sin glifo en la fuente (ignora espacios y controles)."""
        checker = self._checker(buffer)
        return {ch for ch in needed_chars(text) if not checker.has(ch)}

    # -------------------------------------------------------------- helpers
    def _checker(self, buffer: bytes) -> _GlyphChecker:
        digest = hashlib.sha1(buffer).digest()
        checker = self._checkers.get(digest)
        if checker is None:
            checker = _GlyphChecker(buffer)
            self._checkers[digest] = checker
        return checker

    def _covers(self, buffer: bytes, chars: frozenset[str]) -> bool:
        checker = self._checker(buffer)
        return all(checker.has(ch) for ch in chars)

    def _make(
        self, buffer: bytes, ext: str, source: FontSource, clean: str, bold: bool, italic: bool
    ) -> ResolvedFont:
        family = "xpdf_" + hashlib.sha1(buffer).hexdigest()[:12]
        return ResolvedFont(
            css_family=family,
            buffer=buffer,
            filename=f"{family}.{ext}",
            source=source,
            original_name=clean,
            bold=bold,
            italic=italic,
        )

    # ------------------------------------------------------------ incrustada
    def _fonts_on_page(self, page_index: int) -> list[tuple[int, str, str]]:
        """(xref, tipo, basefont normalizado) de las fuentes usadas en la página."""
        cached = self._page_fonts.get(page_index)
        if cached is None:
            cached = []
            if 0 <= page_index < self._doc.page_count:
                seen: set[int] = set()
                for f in self._doc[page_index].get_fonts(full=True):
                    xref, _ext, ftype, basefont = f[0], f[1], f[2], f[3]
                    if xref in seen or xref <= 0:
                        continue
                    seen.add(xref)
                    cached.append((xref, ftype, _norm(clean_font_name(basefont))))
            self._page_fonts[page_index] = cached
        return cached

    def _extract(self, xref: int) -> tuple[str, bytes] | None:
        if xref not in self._embedded:
            try:
                _name, ext, _type, buf = self._doc.extract_font(xref)
            except Exception:
                log.debug("No se pudo extraer la fuente xref %d", xref)
                ext, buf = "n/a", b""
            self._embedded[xref] = (ext, bytes(buf)) if buf and ext != "n/a" else None
        return self._embedded[xref]

    def _same_font_xrefs(self, req: FontRequest, clean: str) -> list[int]:
        """Fuentes incrustadas de la página que son exactamente la del tramo.

        Primero el nombre exacto. Solo si no hay, se acepta que el nombre de
        `get_text` sea un prefijo del de la página (MuPDF trunca nombres largos);
        nunca al revés: «EquityTextA-Italic» no es «EquityTextA».
        """
        target = _norm(clean)
        if not target:
            return []
        fonts = [(x, b) for x, t, b in self._fonts_on_page(req.page_index) if t != "Type3"]
        exact = [x for x, b in fonts if b == target]
        if exact:
            return exact

        def neutral_extra(longer: str, shorter: str) -> bool:
            # «liberationsans» ~ «liberationsansregular», pero no ~ «...bold».
            return longer.startswith(shorter) and longer[len(shorter):] in _NEUTRAL_SUFFIXES

        close = [x for x, b in fonts if neutral_extra(b, target) or neutral_extra(target, b)]
        if close:
            return close
        # El nombre de get_text sale del programa de fuente; la página puede
        # declararla con otro /BaseFont. Se compara con el nombre interno.
        out = []
        for x, _ in fonts:
            extracted = self._extract(x)
            if extracted is None:
                continue
            try:
                inner = _norm(clean_font_name(pymupdf.Font(fontbuffer=extracted[1]).name))
            except Exception:
                continue
            if inner == target or neutral_extra(inner, target) or neutral_extra(target, inner):
                out.append(x)
        if out:
            return out
        # MuPDF trunca nombres muy largos en get_text.
        return [x for x, b in fonts if len(target) >= 24 and b.startswith(target)]

    def embedded_xrefs(self, req: FontRequest) -> list[int]:
        """xrefs de las fuentes de la página que son exactamente la del tramo."""
        return self._same_font_xrefs(req, clean_font_name(req.name))

    def _embedded_style(self, req: FontRequest, clean: str) -> tuple[bool, bool] | None:
        for xref in self._same_font_xrefs(req, clean):
            extracted = self._extract(xref)
            if extracted is None:
                continue
            try:
                f = pymupdf.Font(fontbuffer=extracted[1])
            except Exception:
                continue
            bold, italic = bool(f.is_bold), bool(f.is_italic)
            # Algunas fuentes no declaran estilo en su archivo: entonces manda el nombre.
            named = classify(clean, 0)
            return bold or named.bold, italic or named.italic
        return None

    def _try_embedded(
        self, req: FontRequest, clean: str, style: FontStyle, chars: frozenset[str]
    ) -> ResolvedFont | None:
        for xref in self._same_font_xrefs(req, clean):
            extracted = self._extract(xref)
            if extracted is None:
                continue  # no incrustada (p. ej. base-14)
            ext, buf = extracted
            if self._covers(buf, chars):
                return self._make(buf, _ext_of(buf, ext), "embedded", clean, style.bold, style.italic)
            log.debug("Fuente incrustada %s (xref %d) no cubre el texto", clean, xref)
        return None

    # ------------------------------------------------------------- sistema
    def _index(self) -> SystemFontIndex:
        if self._system_index is None:
            self._system_index = default_system_index()
        return self._system_index

    def _face_bytes(self, face: SystemFace) -> bytes | None:
        key = (face.path, face.index)
        if key not in self._files:
            try:
                data = Path(face.path).read_bytes()
                self._files[key] = extract_ttc_face(data, face.index) if data[:4] == b"ttcf" else data
            except (OSError, ValueError, struct.error):
                log.debug("No se pudo leer %s", face.path)
                return None
        return self._files[key]

    def _system_candidates(self, clean: str, style: FontStyle) -> list[SystemFace]:
        faces = [f for f in self._index().faces() if f.embeddable]
        target = _norm(clean)
        out: list[SystemFace] = []
        # 1) Coincidencia exacta de nombre PostScript o nombre completo, con el
        #    mismo estilo: «Times-Roman» con banderas de negrita debe dar Times Bold.
        out += [
            f for f in faces
            if target and target in (_norm(f.postscript), _norm(f.full))
            and f.bold == style.bold and f.italic == style.italic
        ]  # fmt: skip
        # 2) Familia (y alias) con el mismo estilo negrita/cursiva.
        plain = {"regular", "roman", "normal", "book", "bold", "italic", "oblique", "bolditalic", "boldoblique"}
        for fam in _family_candidates(clean):
            same = [
                f for f in faces
                if _norm(f.family) == fam and f.bold == style.bold and f.italic == style.italic
            ]  # fmt: skip
            # Preferimos caras "normales" frente a Condensed/Light/etc.
            same.sort(key=lambda f: (_norm(f.style) not in plain, len(f.style), f.path, f.index))
            out += [f for f in same if f not in out]
        return out

    def _try_system(self, clean: str, style: FontStyle, chars: frozenset[str]) -> ResolvedFont | None:
        for face in self._system_candidates(clean, style):
            buf = self._face_bytes(face)
            if buf is None or not self._covers(buf, chars):
                continue
            return self._make(buf, _ext_of(buf), "system", clean, face.bold, face.italic)
        return None

    # ------------------------------------------------------------- incluida
    def _bundled(self, clean: str, style: FontStyle, chars: frozenset[str]) -> ResolvedFont:
        path = bundled_font_file(style, self._bundled_dir)
        key = (str(path), 0)
        if key not in self._files:
            self._files[key] = path.read_bytes()
        buf = self._files[key]
        if not self._covers(buf, chars):
            # Se devuelve igualmente: insert_htmlbox dibuja los caracteres que
            # falten con las fuentes de reserva integradas en MuPDF (Noto/Droid).
            log.info("Ninguna fuente cubre todo el texto de %s; MuPDF usará su reserva", clean)
        return self._make(buf, "ttf", "bundled", clean, style.bold, style.italic)
