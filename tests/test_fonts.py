from __future__ import annotations

import os
import shutil
import struct
from pathlib import Path

import pymupdf
import pytest

from xpdf import paths
from xpdf.core import fonts
from xpdf.core.fonts import (
    FontRequest,
    FontResolver,
    ResolvedFont,
    SystemFontIndex,
    classify,
    extract_ttc_face,
    read_font_file,
)

FONT_DIR = paths.assets_dir() / "fonts"
SANS = FONT_DIR / "LiberationSans-Regular.ttf"
SERIF_BOLD = FONT_DIR / "LiberationSerif-Bold.ttf"


# ------------------------------------------------------------------ helpers
def _tables(data: bytes) -> dict[bytes, bytes]:
    num = struct.unpack_from(">H", data, 4)[0]
    out = {}
    for i in range(num):
        tag, _cs, off, length = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        out[tag] = data[off : off + length]
    return out


def _build_sfnt(tables: dict[bytes, bytes], version: bytes = b"\x00\x01\x00\x00") -> bytes:
    tags = sorted(tables)
    num = len(tags)
    header = bytearray(version + struct.pack(">HHHH", num, 0, 0, 0))
    body = bytearray()
    start = 12 + 16 * num
    for tag in tags:
        header += struct.pack(">4sIII", tag, 0, start + len(body), len(tables[tag]))
        body += tables[tag] + b"\x00" * (-len(tables[tag]) % 4)
    return bytes(header + body)


def _cmap_format4(mapping: dict[int, int]) -> bytes:
    """cmap con una sola subtabla (3,1) formato 4: un segmento por carácter."""
    codes = sorted(mapping)
    segs = [(c, c, (mapping[c] - c) & 0xFFFF) for c in codes] + [(0xFFFF, 0xFFFF, 1)]
    n = len(segs)
    sub = bytearray(struct.pack(">HHHHHHH", 4, 0, 0, n * 2, 0, 0, 0))
    sub += b"".join(struct.pack(">H", s[1]) for s in segs) + b"\x00\x00"
    sub += b"".join(struct.pack(">H", s[0]) for s in segs)
    sub += b"".join(struct.pack(">H", s[2]) for s in segs)
    sub += b"\x00\x00" * n
    struct.pack_into(">H", sub, 2, len(sub))
    return struct.pack(">HHHHI", 0, 1, 3, 1, 12) + bytes(sub)


def _partial_font(chars: str, base: Path = SANS) -> bytes:
    """Liberation Sans cuya cmap solo cubre `chars` (simula un subconjunto con cmap)."""
    data = base.read_bytes()
    f = pymupdf.Font(fontbuffer=data)
    mapping = {ord(c): int(f.has_glyph(ord(c))) for c in set(chars)}
    tables = _tables(data)
    tables[b"cmap"] = _cmap_format4(mapping)
    return _build_sfnt(tables)


def _build_ttc(fonts_: list[bytes]) -> bytes:
    n = len(fonts_)
    out = bytearray(b"ttcf" + struct.pack(">HHI", 1, 0, n) + b"\x00" * (4 * n))
    for i, data in enumerate(fonts_):
        struct.pack_into(">I", out, 12 + 4 * i, len(out))
        tables = _tables(data)
        tags = sorted(tables)
        dir_off = len(out)
        out += data[:12] + b"\x00" * (16 * len(tags))
        for j, tag in enumerate(tags):
            off = len(out)
            out += tables[tag] + b"\x00" * (-len(tables[tag]) % 4)
            struct.pack_into(">4sIII", out, dir_off + 12 + 16 * j, tag, 0, off, len(tables[tag]))
    return bytes(out)


def _doc_with_font(buffer: bytes, text: str) -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_font(fontname="F1", fontbuffer=buffer)
    page.insert_text((50, 100), text, fontname="F1", fontsize=12)
    return pymupdf.open("pdf", doc.tobytes())


def _first_span(doc: pymupdf.Document) -> dict:  # type: ignore[type-arg]
    for b in doc[0].get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                return s  # type: ignore[no-any-return]
    raise AssertionError("sin texto")


def _roundtrip(font: ResolvedFont, text: str) -> str:
    doc = pymupdf.open()
    page = doc.new_page()
    arch = pymupdf.Archive()
    arch.add(font.buffer, font.filename)
    css = (
        f"@font-face {{font-family: {font.css_family}; src: url({font.filename});}}"
        f" p {{font-family: {font.css_family}; font-size: 12pt; margin: 0}}"
    )
    spare, _scale = page.insert_htmlbox(
        pymupdf.Rect(50, 50, 550, 400), f"<p>{text}</p>", css=css, archive=arch
    )
    assert spare >= 0
    used = {f[3] for f in page.get_fonts(full=True)}
    assert len(used) == 1, used  # sin fuentes de reserva de MuPDF
    return page.get_text().strip()


@pytest.fixture
def empty_index(tmp_path: Path) -> SystemFontIndex:
    """Índice de sistema vacío: los tests no dependen de la máquina."""
    return SystemFontIndex(dirs=[tmp_path / "no-fonts"], cache_file=tmp_path / "idx.json")


@pytest.fixture
def lib_index(tmp_path: Path) -> SystemFontIndex:
    """'Sistema' con Liberation Serif (regular y negrita) instalada."""
    d = tmp_path / "sysfonts"
    d.mkdir()
    shutil.copy(FONT_DIR / "LiberationSerif-Regular.ttf", d)
    shutil.copy(SERIF_BOLD, d)
    return SystemFontIndex(dirs=[d], cache_file=tmp_path / "idx.json")


# ------------------------------------------------------------ clasificación
@pytest.mark.parametrize(
    ("name", "flags", "cls", "bold", "italic"),
    [
        ("ABCDEF+Calibri-Bold", 0, "sans", True, False),
        ("Helvetica", 4, "sans", False, False),  # flag serif erróneo
        ("Helvetica-BoldOblique", 0, "sans", True, True),
        ("Times-Roman", 0, "serif", False, False),
        ("TimesNewRomanPS-ItalicMT", 0, "serif", False, True),
        ("Arial,BoldItalic", 0, "sans", True, True),
        ("Courier", 0, "mono", False, False),
        ("CourierNewPSMT", 4, "mono", False, False),
        ("Consolas", 0, "mono", False, False),
        ("XYZABC+MinionPro-It", 0, "serif", False, True),
        ("Desconocida", 4 | 16, "serif", True, False),
        ("Desconocida", 8 | 2, "mono", False, True),
        ("Desconocida", 0, "sans", False, False),
        ("Georgia", 0, "serif", False, False),
        ("NotoSans-Regular", 4, "sans", False, False),
    ],
)
def test_classify(name: str, flags: int, cls: str, bold: bool, italic: bool) -> None:
    st = classify(name, flags)
    assert (st.cls, st.bold, st.italic) == (cls, bold, italic)


def test_clean_name() -> None:
    assert fonts.clean_font_name("ABCDEF+Calibri-Bold") == "Calibri-Bold"
    assert fonts.clean_font_name("Calibri") == "Calibri"


# ----------------------------------------------------------- incrustadas
def test_reuses_embedded_when_glyphs_present(empty_index: SystemFontIndex) -> None:
    buf = _partial_font("Hola mundo")
    doc = _doc_with_font(buf, "Hola mundo")
    span = _first_span(doc)
    r = FontResolver(doc, system_index=empty_index)
    res = r.resolve(FontRequest(span["font"], span["flags"], 0), "Hola, mundo")
    # La coma no está en el subconjunto -> no se puede reutilizar.
    assert res.source == "bundled"
    res = r.resolve(FontRequest(span["font"], span["flags"], 0), "mundo Hola")
    assert res.source == "embedded"
    assert not res.substituted
    assert "Liberation" in res.original_name and "+" not in res.original_name
    assert r.missing_glyphs(res.buffer, "Hola\n mundo\t") == set()
    assert r.missing_glyphs(res.buffer, "año €") == {"ñ", "€"}
    assert _roundtrip(res, "Hola mundo") == "Hola mundo"


def test_embedded_missing_glyph_falls_back(lib_index: SystemFontIndex) -> None:
    buf = _partial_font("Hola", base=SERIF_BOLD)
    doc = _doc_with_font(buf, "Hola")
    span = _first_span(doc)
    r = FontResolver(doc, system_index=lib_index)
    res = r.resolve(FontRequest(span["font"], span["flags"], 0), "Hola España €")
    # La misma fuente está "instalada" en el sistema y sí tiene ñ y €.
    assert res.source == "system"
    assert res.bold and not res.italic
    assert pymupdf.Font(fontbuffer=res.buffer).name == "Liberation Serif Bold"
    assert _roundtrip(res, "Hola España €") == "Hola España €"


def test_full_embedded_font_reused_with_accents(empty_index: SystemFontIndex) -> None:
    doc = _doc_with_font(SANS.read_bytes(), "Texto")
    span = _first_span(doc)
    r = FontResolver(doc, system_index=empty_index)
    text = "Canción ÁÉÍÓÚ ñÑ üÜ ç €"
    res = r.resolve(FontRequest(span["font"], span["flags"], 0), text)
    assert res.source == "embedded"
    assert _roundtrip(res, text) == text
    # Caché: misma petición y mismo juego de caracteres -> mismo objeto.
    assert r.resolve(FontRequest(span["font"], span["flags"], 0), text[::-1]) is res


def test_mupdf_subset_without_cmap_is_not_reused(empty_index: SystemFontIndex) -> None:
    """El subsetter de MuPDF elimina la cmap: la fuente no es verificable."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_font(fontname="F1", fontfile=str(SERIF_BOLD))
    page.insert_text((50, 100), "Hola mundo", fontname="F1", fontsize=12)
    doc.subset_fonts()
    doc = pymupdf.open("pdf", doc.tobytes(garbage=3))
    basefont = doc[0].get_fonts(full=True)[0][3]
    assert "+" in basefont  # es un subconjunto
    span = _first_span(doc)
    r = FontResolver(doc, system_index=empty_index)
    res = r.resolve(FontRequest(span["font"], span["flags"], 0), "Hola")
    assert res.source == "bundled"
    assert res.bold
    assert pymupdf.Font(fontbuffer=res.buffer).name == "Liberation Serif Bold"


def test_empty_outline_counts_as_missing(empty_index: SystemFontIndex) -> None:
    """cmap completa pero glifo sin contorno (subconjunto que vacía glifos)."""
    data = SANS.read_bytes()
    gid = int(pymupdf.Font(fontbuffer=data).has_glyph(ord("ñ")))
    tables = _tables(data)
    long_loca = struct.unpack_from(">h", tables[b"head"], 50)[0] == 1
    loca = bytearray(tables[b"loca"])
    if long_loca:
        start = struct.unpack_from(">I", loca, 4 * gid)[0]
        struct.pack_into(">I", loca, 4 * (gid + 1), start)
    else:
        start = struct.unpack_from(">H", loca, 2 * gid)[0]
        struct.pack_into(">H", loca, 2 * (gid + 1), start)
    tables[b"loca"] = bytes(loca)
    broken = _build_sfnt(tables)
    r = FontResolver(pymupdf.open(), system_index=empty_index)
    assert r.missing_glyphs(broken, "año") == {"ñ"}
    assert r.missing_glyphs(data, "año") == set()


def test_type3_and_garbage_are_unusable(empty_index: SystemFontIndex) -> None:
    r = FontResolver(pymupdf.open(), system_index=empty_index)
    assert r.missing_glyphs(b"no es una fuente", "abc") == {"a", "b", "c"}
    assert r.missing_glyphs(b"", " \n\t") == set()


# --------------------------------------------------------- base-14 / incluidas
@pytest.mark.parametrize(
    ("name", "flags", "expected"),
    [
        ("Helvetica", 0, "Liberation Sans Regular"),
        ("Helvetica-Bold", 0, "Liberation Sans Bold"),
        ("Helvetica-Oblique", 0, "Liberation Sans Italic"),
        ("Helvetica-BoldOblique", 0, "Liberation Sans Bold Italic"),
        ("Times-Roman", 0, "Liberation Serif Regular"),
        ("Times-BoldItalic", 0, "Liberation Serif Bold Italic"),
        ("Courier", 0, "Liberation Mono Regular"),
        ("Courier-Bold", 0, "Liberation Mono Bold"),
        ("Fantasia", 8, "Liberation Mono Regular"),
        ("Calibri", 0, "Liberation Sans Regular"),
        ("Cambria-Italic", 0, "Liberation Serif Italic"),
    ],
)
def test_base14_and_unknown_map_to_bundled(
    empty_index: SystemFontIndex, name: str, flags: int, expected: str
) -> None:
    doc = pymupdf.open()
    doc.new_page()
    res = FontResolver(doc, system_index=empty_index).resolve(FontRequest(name, flags, 0), "Año")
    assert res.source == "bundled"
    assert res.substituted
    assert pymupdf.Font(fontbuffer=res.buffer).name == expected
    assert res.bold == ("Bold" in expected)
    assert res.italic == ("Italic" in expected)


def test_base14_non_embedded_in_pdf(empty_index: SystemFontIndex) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 100), "Hola", fontname="tiro", fontsize=12)  # Times-Roman
    span = _first_span(doc)
    res = FontResolver(doc, system_index=empty_index).resolve(
        FontRequest(span["font"], span["flags"], 0), "Adiós"
    )
    assert res.source == "bundled"
    assert pymupdf.Font(fontbuffer=res.buffer).name == "Liberation Serif Regular"


def test_times_maps_to_system_alias(lib_index: SystemFontIndex) -> None:
    """Times-Bold sin Times instalada -> alias Liberation Serif del 'sistema'."""
    r = FontResolver(pymupdf.open(), system_index=lib_index)
    res = r.resolve(FontRequest("Times-Bold", 0, 0), "Año")
    assert res.source == "system" and res.bold and not res.italic
    # No hay cursiva instalada -> no se acepta una cara con otro estilo.
    res = r.resolve(FontRequest("Times-Italic", 0, 0), "Año")
    assert res.source == "bundled" and res.italic


def test_non_latin_and_bundled_last_resort(empty_index: SystemFontIndex) -> None:
    r = FontResolver(pymupdf.open(), system_index=empty_index)
    sans = SANS.read_bytes()
    assert r.missing_glyphs(sans, "Ωλ Жд ĉŝ ő") == set()  # griego, cirílico, Latin Ext
    res = r.resolve(FontRequest("Helvetica", 0, 0), "中文")
    assert res.source == "bundled"  # siempre devuelve algo
    assert r.missing_glyphs(res.buffer, "中文") == {"中", "文"}
    # insert_htmlbox completa con las fuentes de reserva de MuPDF.
    doc = pymupdf.open()
    page = doc.new_page()
    arch = pymupdf.Archive()
    arch.add(res.buffer, res.filename)
    css = f"@font-face {{font-family: {res.css_family}; src: url({res.filename});}} p {{font-family: {res.css_family}}}"
    page.insert_htmlbox(pymupdf.Rect(50, 50, 500, 200), "<p>Año 中文</p>", css=css, archive=arch)
    assert page.get_text().strip() == "Año 中文"
    assert len(page.get_fonts()) == 2


# ---------------------------------------------------------- índice sistema
def test_system_index_cache(tmp_path: Path) -> None:
    d = tmp_path / "fonts"
    (d / "sub").mkdir(parents=True)
    shutil.copy(SANS, d)
    shutil.copy(SERIF_BOLD, d / "sub")
    cache = tmp_path / "data" / "font-index.json"
    cache.parent.mkdir()

    idx = SystemFontIndex(dirs=[d], cache_file=cache)
    faces = idx.faces()
    assert idx.scanned_files == 2 and cache.is_file()
    assert {f.postscript for f in faces} == {"LiberationSans", "LiberationSerif-Bold"}
    assert idx.faces() is faces  # en memoria

    idx2 = SystemFontIndex(dirs=[d], cache_file=cache)
    assert len(idx2.faces()) == 2 and idx2.scanned_files == 0  # desde JSON

    shutil.copy(FONT_DIR / "LiberationMono-Italic.ttf", d / "sub")
    os.utime(d / "sub", (1, 1))  # garantiza el cambio de mtime de la carpeta
    idx3 = SystemFontIndex(dirs=[d], cache_file=cache)
    faces3 = idx3.faces()
    assert idx3.scanned_files == 1  # solo el archivo nuevo
    mono = next(f for f in faces3 if f.postscript == "LiberationMono-Italic")
    assert mono.mono and mono.italic and not mono.bold

    # Otras carpetas raíz -> no se reutiliza la caché.
    idx4 = SystemFontIndex(dirs=[d / "sub"], cache_file=cache)
    assert len(idx4.faces()) == 2


def test_default_cache_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "user_data_dir", lambda: tmp_path)
    idx = SystemFontIndex(dirs=[tmp_path / "nada"])
    assert idx.faces() == []
    assert (tmp_path / "font-index.json").is_file()


def test_corrupt_cache_is_rebuilt(tmp_path: Path) -> None:
    cache = tmp_path / "idx.json"
    cache.write_text("{no json", "utf-8")
    d = tmp_path / "f"
    d.mkdir()
    shutil.copy(SANS, d)
    assert len(SystemFontIndex(dirs=[d], cache_file=cache).faces()) == 1


def test_ttc_collection(tmp_path: Path) -> None:
    regular = SANS.read_bytes()
    bold = (FONT_DIR / "LiberationSans-Bold.ttf").read_bytes()
    d = tmp_path / "fonts"
    d.mkdir()
    (d / "Coleccion.ttc").write_bytes(_build_ttc([regular, bold]))
    faces = read_font_file(str(d / "Coleccion.ttc"))
    assert [(f.index, f.bold) for f in faces] == [(0, False), (1, True)]

    face1 = extract_ttc_face((d / "Coleccion.ttc").read_bytes(), 1)
    assert pymupdf.Font(fontbuffer=face1).name == "Liberation Sans Bold"

    idx = SystemFontIndex(dirs=[d], cache_file=tmp_path / "i.json")
    r = FontResolver(pymupdf.open(), system_index=idx)
    res = r.resolve(FontRequest("Arial-BoldMT", 0, 0), "Ñandú")
    assert res.source == "system" and res.bold
    assert res.filename.endswith(".ttf")
    assert pymupdf.Font(fontbuffer=res.buffer).name == "Liberation Sans Bold"
    assert _roundtrip(res, "Ñandú") == "Ñandú"


def test_restricted_fonts_are_skipped(tmp_path: Path) -> None:
    data = bytearray(SANS.read_bytes())
    num = struct.unpack_from(">H", data, 4)[0]
    for i in range(num):
        tag, _cs, off, _ln = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        if tag == b"OS/2":
            struct.pack_into(">H", data, off + 8, 0x0002)
    d = tmp_path / "f"
    d.mkdir()
    (d / "r.ttf").write_bytes(bytes(data))
    idx = SystemFontIndex(dirs=[d], cache_file=tmp_path / "i.json")
    assert not idx.faces()[0].embeddable
    res = FontResolver(pymupdf.open(), system_index=idx).resolve(
        FontRequest("LiberationSans", 0, 0), "a"
    )
    assert res.source == "bundled"


# ------------------------------------------------------------ smoke real
def test_real_system_smoke() -> None:
    idx = fonts.default_system_index()
    names = {f.family.lower() for f in idx.faces()}
    if not names & {"helvetica", "arial", "liberation sans", "times", "times new roman"}:
        pytest.skip("No hay fuentes de sistema conocidas")
    r = FontResolver(pymupdf.open())
    for req in ("Helvetica", "Times-Bold", "Arial-ItalicMT"):
        res = r.resolve(FontRequest(req, 0, 0), "Canción")
        assert _roundtrip(res, "Canción") == "Canción"


def test_exact_system_name_respects_requested_style() -> None:
    """«Times-Roman» con bandera de negrita no debe devolver la cara normal."""
    import pymupdf as _pm

    from xpdf.core.fonts import FontRequest as _Req, FontResolver as _Res

    d = _pm.open()
    d.new_page()
    r = _Res(d)
    for name, flags, bold, italic in [
        ("Times-Roman", 4 | 16, True, False),
        ("Helvetica", 2, False, True),
        ("Courier", 8 | 16 | 2, True, True),
    ]:
        rf = r.resolve(_Req(name, flags, 0), "Texto ñ")
        assert (rf.bold, rf.italic) == (bold, italic), (name, rf)
