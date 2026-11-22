from __future__ import annotations

import pymupdf
import pytest

from tests.text_helpers import FakeResolver
from xpdf.core.text import (
    Run,
    TextEditError,
    add_text,
    detect_paragraphs,
    paragraph_at,
    replace_paragraph,
    restyle,
)

LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor "
    "incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud "
    "exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat."
)


def _page_with(html: str, css: str = "", rect: tuple[float, float, float, float] = (72, 72, 400, 700)) -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_htmlbox(pymupdf.Rect(rect), html, css="* {font-family: sans-serif; font-size: 11px;} p {margin-bottom: 12px;} " + css)
    return pymupdf.open("pdf", doc.tobytes())


def _pix(doc: pymupdf.Document, i: int = 0, clip: pymupdf.Rect | None = None) -> pymupdf.Pixmap:
    return doc[i].get_pixmap(dpi=100, clip=clip)


def _diff(a: pymupdf.Pixmap, b: pymupdf.Pixmap, tol: int = 40) -> float:
    assert (a.width, a.height) == (b.width, b.height)
    sa, sb = a.samples, b.samples
    n = a.n
    bad = 0
    total = a.width * a.height
    for i in range(0, len(sa), n):
        if max(abs(sa[i + k] - sb[i + k]) for k in range(min(n, 3))) > tol:
            bad += 1
    return bad / total


# ------------------------------------------------------------------ detección
def test_detects_paragraphs_and_alignment() -> None:
    doc = _page_with(
        f"<h2 style='font-size:18px'>Título del documento</h2>"
        f"<p style='text-align:justify'>{LOREM}</p>"
        f"<p>{LOREM}</p>"
        f"<p style='text-align:center'>{LOREM}</p>"
        f"<p style='text-align:right'>{LOREM}</p>"
    )
    paras = detect_paragraphs(doc, 0)
    assert [p.text[:7] for p in paras] == ["Título ", "Lorem i", "Lorem i", "Lorem i", "Lorem i"]
    assert [p.align for p in paras[1:]] == ["justify", "left", "center", "right"]
    assert paras[1].text == LOREM
    assert len(paras[1].lines) >= 3
    assert paras[1].line_pitch == pytest.approx(paras[2].line_pitch, abs=0.5)


def test_two_columns_are_separate() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    css = "* {font-family: serif; font-size: 10px; text-align: justify}"
    page.insert_htmlbox(pymupdf.Rect(50, 72, 290, 500), f"<p>{LOREM}</p>", css=css)
    page.insert_htmlbox(pymupdf.Rect(305, 72, 545, 500), f"<p>{LOREM[::-1]}</p>", css=css)
    paras = detect_paragraphs(doc, 0)
    assert len(paras) == 2
    assert paras[0].bbox[2] < 300 < paras[1].bbox[0]


def test_inline_bold_and_hyphenation() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Una palabra par-", fontname="helv", fontsize=12)
    page.insert_text((72, 114), "tida y otra ", fontname="helv", fontsize=12)
    page.insert_text((134, 114), "negrita", fontname="hebo", fontsize=12)
    page.insert_text((72, 128), "auto-gestión final.", fontname="helv", fontsize=12)
    paras = detect_paragraphs(doc, 0)
    assert len(paras) == 1
    p = paras[0]
    assert p.text == "Una palabra partida y otra negrita auto-gestión final."
    bold = [r for r in p.runs if r.style.bold]
    assert [r.text.strip() for r in bold] == ["negrita"]


def test_paragraph_at() -> None:
    doc = _page_with(f"<p>{LOREM}</p><p>Segundo</p>")
    paras = detect_paragraphs(doc, 0)
    x0, y0, x1, y1 = paras[1].bbox
    assert paragraph_at(paras, (x0 + x1) / 2, (y0 + y1) / 2) is paras[1]
    assert paragraph_at(paras, 10, 10) is None


# -------------------------------------------------------------------- edición
def test_rewrite_unchanged_is_visually_identical() -> None:
    doc = _page_with(f"<p style='text-align:justify'>{LOREM}</p><p>Otro párrafo debajo.</p>")
    before = pymupdf.open("pdf", doc.tobytes())
    paras = detect_paragraphs(doc, 0)
    p = paras[0]
    res = replace_paragraph(doc, p, p.runs, FakeResolver())
    after_paras = detect_paragraphs(doc, 0)
    assert after_paras[0].text == LOREM
    assert after_paras[0].align == "justify"
    assert len(after_paras[0].lines) == len(p.lines)
    # misma línea base y mismo margen izquierdo
    assert after_paras[0].first_baseline == pytest.approx(p.first_baseline, abs=0.3)
    assert after_paras[0].bbox[0] == pytest.approx(p.bbox[0], abs=0.5)
    # fuera del párrafo, idéntico
    r = pymupdf.Rect(p.bbox) + (-2, -2, 2, 2)
    below = pymupdf.Rect(0, r.y1 + 1, 595, 842)
    assert _diff(_pix(before, clip=below), _pix(doc, clip=below)) == 0
    # dentro, casi idéntico (misma fuente base)
    assert _diff(_pix(before, clip=r), _pix(doc, clip=r)) < 0.02
    assert res.bbox[1] >= r.y0


def test_longer_text_grows_downward_and_keeps_width() -> None:
    doc = _page_with(f"<p>{LOREM}</p>")
    p = detect_paragraphs(doc, 0)[0]
    new = [Run(LOREM + " " + LOREM, p.runs[0].style)]
    res = replace_paragraph(doc, p, new, FakeResolver())
    q = detect_paragraphs(doc, 0)
    assert len(q) == 1
    assert q[0].text == LOREM + " " + LOREM
    assert q[0].bbox[2] <= p.bbox[2] + 1
    assert q[0].bbox[3] > p.bbox[3] + 20
    assert res.bbox[3] == pytest.approx(q[0].bbox[3], abs=1)


def test_accents_and_inline_styles_survive() -> None:
    doc = _page_with("<p>Texto original.</p>")
    p = detect_paragraphs(doc, 0)[0]
    st = p.runs[0].style
    runs = [
        Run("Pingüino ñandú ", st),
        Run("acción", restyle([Run("", st)], flags=st.flags | 16)[0].style),
        Run(" — «comillas» €.", st),
    ]
    replace_paragraph(doc, p, runs, FakeResolver())
    q = detect_paragraphs(doc, 0)[0]
    assert q.text == "Pingüino ñandú acción — «comillas» €."
    assert [r.text.strip() for r in q.runs if r.style.bold] == ["acción"]


def test_background_graphics_and_images_preserved() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(pymupdf.Rect(60, 60, 420, 200), color=None, fill=(1, 0.9, 0.6))
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False)
    pix.set_rect(pix.irect, (0, 120, 255))
    page.insert_image(pymupdf.Rect(300, 80, 340, 120), pixmap=pix)
    page.insert_htmlbox(pymupdf.Rect(72, 72, 400, 190), f"<p>{LOREM}</p>", css="*{font-size:11px}")
    doc = pymupdf.open("pdf", doc.tobytes())
    p = detect_paragraphs(doc, 0)[0]
    replace_paragraph(doc, p, [Run("Corto.", p.runs[0].style)], FakeResolver())
    page = doc[0]
    # el fondo sigue debajo de donde estaba el texto y la imagen sigue entera
    px = page.get_pixmap(dpi=72)
    def close(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
        return all(abs(x - y) <= 2 for x, y in zip(a, b))

    assert close(px.pixel(200, 150)[:3], (255, 230, 153))
    assert close(px.pixel(330, 110)[:3], (0, 120, 255))
    assert len(page.get_images()) == 1


def test_neighbour_lines_untouched_with_tight_leading() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Primera línea apretada", fontsize=14)
    page.insert_text((72, 110), "Segunda línea, otro párrafo", fontsize=9)
    doc = pymupdf.open("pdf", doc.tobytes())
    paras = detect_paragraphs(doc, 0)
    assert len(paras) == 2
    replace_paragraph(doc, paras[0], [Run("Cambiada", paras[0].runs[0].style)], FakeResolver())
    text = doc[0].get_text()
    assert "Segunda línea, otro párrafo" in text
    assert "Cambiada" in text and "Primera" not in text


def test_overflow_raises_and_leaves_page_untouched() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((20, 180), "Al pie de la página", fontsize=12)
    doc = pymupdf.open("pdf", doc.tobytes())
    before = doc[0].read_contents()
    p = detect_paragraphs(doc, 0)[0]
    with pytest.raises(TextEditError, match="no cabe"):
        replace_paragraph(doc, p, [Run(LOREM * 3, p.runs[0].style)], FakeResolver())
    assert doc[0].get_text().strip() == "Al pie de la página"
    assert doc[0].read_contents() == before


def test_delete_paragraph_with_empty_text() -> None:
    doc = _page_with(f"<p>{LOREM}</p><p>Queda.</p>")
    p = detect_paragraphs(doc, 0)[0]
    replace_paragraph(doc, p, [Run("  ", p.runs[0].style)], FakeResolver())
    assert doc[0].get_text().strip() == "Queda."


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_edit_on_rotated_page(rotation: int) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.set_rotation(rotation)
    # Escribir texto que se vea horizontal en la página rotada.
    vis = pymupdf.Rect(72, 72, 400, 300) * ~page.rotation_matrix
    page.insert_htmlbox(vis, f"<p>{LOREM}</p>", css="*{font-size:11px}", rotate=rotation)
    doc = pymupdf.open("pdf", doc.tobytes())
    paras = detect_paragraphs(doc, 0)
    assert len(paras) == 1, [p.text for p in paras]
    p = paras[0]
    assert p.bbox[0] == pytest.approx(72, abs=3) and p.bbox[1] == pytest.approx(72, abs=8)
    replace_paragraph(doc, p, [Run("Editado en página rotada.", p.runs[0].style)], FakeResolver())
    q = detect_paragraphs(doc, 0)
    assert [x.text for x in q] == ["Editado en página rotada."]
    assert q[0].first_baseline == pytest.approx(p.first_baseline, abs=0.5)


def test_add_text_at_point() -> None:
    doc = pymupdf.open()
    doc.new_page()
    from xpdf.core.text import TextStyle

    st = TextStyle("Helvetica", 0, 14, 0xCC0000)
    res = add_text(doc, 0, 100, 200, [Run("Nuevo texto\nsegunda línea ñ", st)], FakeResolver())
    paras = detect_paragraphs(doc, 0)
    assert [p.text for p in paras] == ["Nuevo texto segunda línea ñ"]
    assert paras[0].bbox[0] == pytest.approx(100, abs=1.5)
    assert paras[0].bbox[1] == pytest.approx(200, abs=3)
    assert paras[0].runs[0].style.color == 0xCC0000
    assert res.bbox[1] == pytest.approx(200, abs=3)


@pytest.mark.parametrize("family", ["sans-serif", "serif", "monospace"])
@pytest.mark.parametrize("align", ["justify", "left", "center", "right"])
def test_real_resolver_unchanged_rewrite_is_faithful(family: str, align: str) -> None:
    from xpdf.core.fonts import FontResolver

    doc = _page_with(f"<p style='text-align:{align}; font-family:{family}'>{LOREM}</p>")
    before = pymupdf.open("pdf", doc.tobytes())
    p = detect_paragraphs(doc, 0)[0]
    assert p.align == align
    words_before = before[0].get_text("words")
    replace_paragraph(doc, p, p.runs, FontResolver(doc))
    q = detect_paragraphs(doc, 0)[0]
    assert q.text == p.text and len(q.lines) == len(p.lines) and q.align == align
    # Criterio geométrico: cada palabra en su sitio (< 0,5 pt).
    words_after = doc[0].get_text("words")
    assert [w[4] for w in words_after] == [w[4] for w in words_before]
    for a, b in zip(words_before, words_after):
        assert max(abs(a[i] - b[i]) for i in range(4)) < 0.5, (a, b)
    # Y a la vista, casi idéntico (el antialiasing subpíxel varía un poco).
    r = pymupdf.Rect(p.bbox) + (-2, -2, 2, 2)
    assert _diff(_pix(before, clip=r), _pix(doc, clip=r)) < 0.15


def test_growing_paragraph_reports_overlap() -> None:
    doc = _page_with(f"<p>{LOREM}</p><p>Texto de debajo que no se debe tapar.</p>")
    p = detect_paragraphs(doc, 0)[0]
    res = replace_paragraph(doc, p, [Run(LOREM * 2, p.runs[0].style)], FakeResolver())
    assert res.overlaps
    q = detect_paragraphs(doc, 0)[0]
    res2 = replace_paragraph(doc, q, [Run("Corto.", q.runs[0].style)], FakeResolver())
    assert not res2.overlaps


# ------------------------------------------------- agrupación (regresiones)
def test_rule_line_is_not_merged_with_text() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((200, 100), "No. 23-50697", fontsize=12, fontname="tiro")
    page.insert_text((180, 114), "________________", fontsize=12, fontname="tiro")
    paras = detect_paragraphs(doc, 0)
    assert [p.text for p in paras] == ["No. 23-50697", "________________"]


def test_short_line_ends_paragraph_forced_break() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Before Jones, Smith, and Ho, Circuit Judges.", fontsize=12, fontname="tiro")
    page.insert_text((72, 115), "James C. Ho, Circuit Judge:", fontsize=12, fontname="tiro")
    page.insert_htmlbox(pymupdf.Rect(72, 125, 500, 300), f"<p>{LOREM}</p>", css="*{font-family:serif;font-size:12px}")
    paras = detect_paragraphs(doc, 0)
    texts = [p.text for p in paras]
    assert texts[0] == "Before Jones, Smith, and Ho, Circuit Judges."
    assert texts[1] == "James C. Ho, Circuit Judge:"
    assert texts[2] == LOREM


def test_other_font_family_is_not_merged() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((300, 100), "Clerk", fontsize=11, fontname="helv")
    page.insert_text((180, 114), "Plaintiffs, Appellants", fontsize=12, fontname="tiro")
    assert len(detect_paragraphs(doc, 0)) == 2


def test_whole_bold_heading_not_merged_with_body() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "1. OVERVIEW", fontsize=12, fontname="tibo")
    page.insert_text((72, 114), "While AI is improving operations and service delivery across the", fontsize=12, fontname="tiro")
    assert len(detect_paragraphs(doc, 0)) == 2


def test_single_line_grows_horizontally_and_keeps_style() -> None:
    doc = _page_with(
        f"<p style='text-align:justify'>{LOREM}</p><p style='text-align:center'><i>versus</i></p>"
        f"<p style='text-align:justify'>{LOREM}</p>"
    )
    paras = detect_paragraphs(doc, 0)
    v = next(p for p in paras if p.text == "versus")
    assert v.align == "center"
    assert v.box[1] - v.box[0] > 3 * (v.bbox[2] - v.bbox[0])
    st = v.runs[0].style
    replace_paragraph(doc, v, [Run("versus con más texto", st)], FakeResolver())
    after = next(p for p in detect_paragraphs(doc, 0) if p.text.startswith("versus"))
    assert after.text == "versus con más texto"
    assert len(after.lines) == 1
    assert after.first_baseline == pytest.approx(v.first_baseline, abs=0.3)
    assert all(r.style.italic for r in after.runs)
    # sigue centrada respecto al mismo eje
    assert (after.bbox[0] + after.bbox[2]) / 2 == pytest.approx((v.bbox[0] + v.bbox[2]) / 2, abs=1.5)


def test_move_paragraph_keeps_line_breaks() -> None:
    doc = _page_with(f"<p style='text-align:justify'>{LOREM}</p>")
    p = detect_paragraphs(doc, 0)[0]
    replace_paragraph(doc, p, p.runs, FakeResolver(), offset=(30.0, 200.0))
    q = detect_paragraphs(doc, 0)
    assert len(q) == 1 and q[0].text == LOREM
    assert len(q[0].lines) == len(p.lines)  # mismos cortes de línea
    assert q[0].bbox[0] == pytest.approx(p.bbox[0] + 30, abs=0.6)
    assert q[0].first_baseline == pytest.approx(p.first_baseline + 200, abs=0.3)
    assert not doc[0].get_text("text", clip=pymupdf.Rect(p.bbox)).strip()  # el sitio viejo, vacío


def test_move_paragraph_outside_page_fails() -> None:
    doc = _page_with(f"<p>{LOREM}</p>")
    p = detect_paragraphs(doc, 0)[0]
    with pytest.raises(TextEditError, match="saldría"):
        replace_paragraph(doc, p, p.runs, FakeResolver(), offset=(-200.0, 0.0))
    assert detect_paragraphs(doc, 0)[0].bbox[0] == pytest.approx(p.bbox[0], abs=0.1)  # sin tocar
