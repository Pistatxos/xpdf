from __future__ import annotations

from pathlib import Path

import pymupdf

from xpdf.core.document import Document
from xpdf.core.editing import NewTextStyle, Span, TextEditing
from xpdf.core.history import History

LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor "
    "incididunt ut labore et dolore magna aliqua."
)


def _doc(tmp_path: Path) -> Document:
    d = pymupdf.open()
    page = d.new_page()
    page.insert_htmlbox(
        pymupdf.Rect(72, 72, 400, 400),
        f"<p>{LOREM}</p><p>Segundo <b>párrafo</b> con negrita.</p>",
        css="* {font-family: sans-serif; font-size: 11px} p {margin-bottom: 10px}",
    )
    path = tmp_path / "in.pdf"
    d.save(path)
    return Document.open(path)


def test_paragraph_infos(tmp_path: Path) -> None:
    ed = TextEditing(_doc(tmp_path))
    paras = ed.paragraphs(0)
    assert [p.text for p in paras] == [LOREM, "Segundo párrafo con negrita."]
    second = paras[1]
    assert [second.styles[i].bold for _, i in second.spans] == [False, True, False]
    assert all(st.font_data for st in second.styles)
    hit = ed.paragraph_at(0, *second.line_rects[0][:2])
    assert hit is not None and hit.id == second.id


def test_edit_undo_redo_and_save(tmp_path: Path) -> None:
    doc = _doc(tmp_path)
    ed = TextEditing(doc)
    h = History()
    p = ed.paragraphs(0)[1]
    spans = [Span(t, i) for t, i in p.spans]
    spans[0] = Span("Primer ", spans[0].style)
    cmd = ed.edit_command(0, p.id, spans)
    h.push(cmd)
    assert cmd.result is not None and not cmd.result.substitutions
    assert ed.paragraphs(0)[1].text == "Primer párrafo con negrita."
    h.undo()
    assert ed.paragraphs(0)[1].text == "Segundo párrafo con negrita."
    h.redo()
    assert ed.paragraphs(0)[1].text == "Primer párrafo con negrita."
    out = tmp_path / "out.pdf"
    doc.save_as(out)
    reopened = TextEditing(Document.open(out))
    assert reopened.paragraphs(0)[1].text == "Primer párrafo con negrita."


def test_toggle_bold_and_size(tmp_path: Path) -> None:
    ed = TextEditing(_doc(tmp_path))
    p = ed.paragraphs(0)[1]
    spans = [Span(t, i, bold=True, size=14) for t, i in p.spans]
    History().push(ed.edit_command(0, p.id, spans))
    q = ed.paragraphs(0)[1]
    assert q.text == p.text
    assert all(q.styles[i].bold for _, i in q.spans)
    assert all(abs(q.styles[i].size - 14) < 0.1 for _, i in q.spans)


def test_add_text_command(tmp_path: Path) -> None:
    doc = _doc(tmp_path)
    ed = TextEditing(doc)
    h = History()
    h.push(ed.add_command(0, 100, 600, "Añadido ñ €", NewTextStyle("serif", 16, 0x0000FF, bold=True)))
    texts = [p.text for p in ed.paragraphs(0)]
    assert "Añadido ñ €" in texts
    new = next(p for p in ed.paragraphs(0) if p.text == "Añadido ñ €")
    st = new.styles[new.spans[0][1]]
    assert st.color == 0x0000FF and st.bold and st.font_class == "serif"
    h.undo()
    assert "Añadido ñ €" not in [p.text for p in ed.paragraphs(0)]


def test_render_without_hides_only_that_paragraph(tmp_path: Path) -> None:
    doc = _doc(tmp_path)
    ed = TextEditing(doc)
    p = ed.paragraphs(0)[0]
    full = doc.render_page(0, 1.0)
    masked = ed.render_without(0, p.id, 1.0)
    assert (masked.width, masked.height) == (full.width, full.height)
    assert masked.samples != full.samples
    # el documento real no cambia
    assert ed.paragraphs(0)[0].text == LOREM


def test_user_can_widen_and_narrow_the_box(tmp_path: Path) -> None:
    d = pymupdf.open()
    d.new_page().insert_htmlbox(
        pymupdf.Rect(72, 72, 400, 300), f"<p>{LOREM}</p>", css="* {font-family: sans-serif; font-size: 11px}"
    )
    path = tmp_path / "solo.pdf"
    d.save(path)
    ed = TextEditing(Document.open(path))
    h = History()
    p = ed.paragraphs(0)[0]
    n_lines = len(p.line_rects)
    x0, _, x1, _ = p.bbox
    # más estrecha: más líneas
    h.push(ed.edit_command(0, p.id, [Span(t, i) for t, i in p.spans], box=(x0, x0 + (x1 - x0) / 2)))
    q = ed.paragraphs(0)[0]
    assert q.text == p.text and len(q.line_rects) > n_lines
    assert q.bbox[2] <= x0 + (x1 - x0) / 2 + 1
    # más ancha: menos líneas
    h.push(ed.edit_command(0, q.id, [Span(t, i) for t, i in q.spans], box=(x0, 560)))
    r = ed.paragraphs(0)[0]
    assert r.text == p.text and len(r.line_rects) < len(q.line_rects) and r.bbox[2] > x1 + 50


def test_add_text_with_width_wraps(tmp_path: Path) -> None:
    ed = TextEditing(_doc(tmp_path))
    History().push(ed.add_command(0, 100, 600, "uno dos tres cuatro cinco seis siete", NewTextStyle(size=12), width=80))
    new = next(p for p in ed.paragraphs(0) if p.text.startswith("uno"))
    assert len(new.line_rects) >= 3 and new.bbox[2] <= 181
