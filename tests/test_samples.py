"""Baseline checks for the PDF corpus in tests/samples/ and for tests/visual.py."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pymupdf
import pytest

from tests.conftest import SAMPLES_DIR, SampleInfo
from tests.visual import diff_ratio, dump_if_requested, render, save_side_by_side

# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------


def test_manifest_matches_directory(samples: list[SampleInfo]) -> None:
    listed = {s.file for s in samples}
    on_disk = {p.name for p in SAMPLES_DIR.glob("*.pdf")}
    assert listed == on_disk
    assert len(listed) == len(samples), "duplicate manifest entries"


def test_corpus_size_budget() -> None:
    total = sum(p.stat().st_size for p in SAMPLES_DIR.glob("*.pdf"))
    assert total < 8 * 1024 * 1024


def test_corpus_covers_required_categories(samples: list[SampleInfo]) -> None:
    cats = {s.category for s in samples}
    assert {"word", "latex", "scan-ocr", "form", "non-latin", "background", "rotated"} <= cats
    tags = set().union(*(s.tags for s in samples))
    assert {"type3", "cid-identity-h", "render-mode-3", "page-rotation", "justified"} <= tags


def test_opens_and_page_count(sample: SampleInfo) -> None:
    with pymupdf.open(sample.path) as doc:
        assert doc.is_pdf
        assert not doc.needs_pass
        assert doc.page_count == sample.page_count


def test_renders_without_error(sample: SampleInfo) -> None:
    with pymupdf.open(sample.path) as doc:
        for i in range(doc.page_count):
            r = render(doc, i, dpi=50)
            assert r.width > 0 and r.height > 0
            # Not a blank page: at least some non-white pixels.
            assert set(r.pix.samples[::997]) != {255}, f"page {i} rendered blank"


def test_text_extraction(sample: SampleInfo) -> None:
    with pymupdf.open(sample.path) as doc:
        for i, page in enumerate(doc):
            words = page.get_text("words")
            assert len(words) >= sample.min_words[i], f"page {i}: {len(words)} words"


def test_text_dict_has_spans_with_fonts(sample: SampleInfo) -> None:
    """The paragraph detector relies on get_text('dict'): every span must carry
    a font name, size and bbox."""
    with pymupdf.open(sample.path) as doc:
        for page in doc:
            for b in page.get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    assert len(line["dir"]) == 2
                    for span in line["spans"]:
                        assert span["size"] > 0
                        assert isinstance(span["font"], str)
                        assert pymupdf.Rect(span["bbox"]).is_valid


@pytest.mark.samples("render-mode-3")
def test_ocr_text_is_invisible(sample: SampleInfo) -> None:
    with pymupdf.open(sample.path) as doc:
        modes = {t["type"] for t in doc[0].get_texttrace()}
    assert modes == {3}


@pytest.mark.samples("form")
def test_form_has_widgets(sample: SampleInfo) -> None:
    with pymupdf.open(sample.path) as doc:
        assert doc.is_form_pdf
        kinds = {w.field_type_string for w in doc[0].widgets()}
    assert {"Text", "CheckBox"} <= kinds


@pytest.mark.samples("type3")
def test_type3_fonts_present(sample: SampleInfo) -> None:
    with pymupdf.open(sample.path) as doc:
        types = {f[2] for f in doc.get_page_fonts(0)}
    assert "Type3" in types


@pytest.mark.samples("page-rotation")
def test_rotated_pages(sample: SampleInfo) -> None:
    with pymupdf.open(sample.path) as doc:
        assert [p.rotation for p in doc] == [90, 180, 270]


@pytest.mark.samples("vertical-writing")
def test_vertical_writing_direction(sample: SampleInfo) -> None:
    with pymupdf.open(sample.path) as doc:
        dirs = {tuple(round(v) for v in line["dir"]) for b in doc[0].get_text("dict")["blocks"] for line in b.get("lines", [])}
    assert dirs == {(0, 1)}


def test_synthetic_ground_truth() -> None:
    from tests.samples import make_samples as ms

    with pymupdf.open(SAMPLES_DIR / "synth_ocr_scan.pdf") as doc:
        text = doc[0].get_text()
    for line in ms.OCR_LINES:
        if line:
            assert line in text
    with pymupdf.open(SAMPLES_DIR / "synth_text_over_background.pdf") as doc:
        flat = " ".join(doc[0].get_text().split())
    assert ms.BACKGROUND_TEXT in flat


def _largest_block(page: pymupdf.Page) -> pymupdf.Rect:
    blocks = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
    b = max(blocks, key=lambda b: sum(len(s["text"]) for line in b["lines"] for s in line["spans"]))
    return pymupdf.Rect(b["bbox"])


def test_safe_redaction_keeps_rest_of_page_identical(sample: SampleInfo, visual_dump: Callable[..., Path | None]) -> None:
    """Baseline for the editor: redacting a text block with images/graphics
    left alone changes no pixel outside its bbox, on every sample."""
    with pymupdf.open(sample.path) as doc:
        page = doc[0]
        bbox = _largest_block(page)
        before = render(doc, 0)
        page.add_redact_annot(bbox, fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        after = render(doc, 0)
    visual_dump(before, after, bbox)
    assert diff_ratio(before, after, bbox, outside=True) == 0.0
    inside = diff_ratio(before, after, bbox)
    if sample.has("render-mode-3"):
        assert inside == 0.0  # the OCR layer is invisible: nothing visible changes
    else:
        assert inside > 0.01


def test_default_redaction_damages_background() -> None:
    """Trap documented for the text engine: apply_redactions() defaults blank
    images and line art under the bbox. Always pass images/graphics=NONE."""
    path = SAMPLES_DIR / "synth_text_over_background.pdf"
    results = {}
    for safe in (True, False):
        with pymupdf.open(path) as doc:
            page = doc[0]
            bbox = [pymupdf.Rect(b[:4]) for b in page.get_text("blocks") if "Redacting" in b[4]][0]
            before = render(doc, 0)
            page.add_redact_annot(bbox, fill=False)
            if safe:
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
            else:
                page.apply_redactions()
            results[safe] = diff_ratio(before, render(doc, 0), bbox)
    assert results[False] > 2 * results[True]


# --------------------------------------------------------------------------
# visual.py self-tests
# --------------------------------------------------------------------------


def _doc_with_text(rotation: int = 0) -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.draw_rect(pymupdf.Rect(0, 0, 300, 200), color=None, fill=(0.85, 0.9, 1.0))
    page.insert_text((20, 40), "Hello visual diff", fontsize=14)
    page.set_rotation(rotation)
    return doc


def test_identical_renders_have_zero_diff() -> None:
    a = render(_doc_with_text(), 0)
    b = render(_doc_with_text(), 0)
    assert diff_ratio(a, b) == 0.0
    assert diff_ratio(a, b, pymupdf.Rect(10, 10, 100, 100)) == 0.0
    assert diff_ratio(a, b, pymupdf.Rect(10, 10, 100, 100), outside=True) == 0.0


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_drawn_rect_only_differs_inside(rotation: int, tmp_path: Path) -> None:
    doc = _doc_with_text(rotation)
    before = render(doc, 0)
    box = pymupdf.Rect(150, 100, 250, 160)  # unrotated page space
    doc[0].draw_rect(box, color=None, fill=(1, 0, 0))
    after = render(doc, 0)
    assert diff_ratio(before, after, box, outside=True) == 0.0
    inside = diff_ratio(before, after, box)
    assert inside > 0.97
    whole = diff_ratio(before, after)
    assert 0 < whole < inside
    out = tmp_path / "sbs.png"
    save_side_by_side(before, after, out, box)
    pix = pymupdf.Pixmap(str(out))
    assert pix.width > 3 * before.width and pix.height == before.height
    dump_if_requested(f"visual_selftest_rot{rotation}", before, after, box)


def test_antialias_tolerance_and_margin() -> None:
    """A glyph rendered at a slightly different position differs mostly by
    anti-aliasing; the region/margin knobs keep 'outside' checks exact."""
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((20.0, 40.0), "Shift", fontsize=14)
    before = render(doc, 0)
    doc2 = pymupdf.open()
    doc2.new_page(width=300, height=200).insert_text((20.2, 40.0), "Shift", fontsize=14)
    after = render(doc2, 0)
    bbox = pymupdf.Rect(18, 25, 70, 45)
    assert diff_ratio(before, after, bbox, outside=True, margin=1) == 0.0
    loose = diff_ratio(before, after, bbox, tol=120)
    strict = diff_ratio(before, after, bbox, tol=0)
    assert loose <= strict


def test_size_mismatch_raises() -> None:
    a = render(_doc_with_text(), 0, dpi=72)
    b = render(_doc_with_text(), 0, dpi=100)
    with pytest.raises(ValueError):
        diff_ratio(a, b)


def test_render_accepts_path_page_and_bytes() -> None:
    p = SAMPLES_DIR / "synth_rotated_pages.pdf"
    with pymupdf.open(p) as doc:
        r1 = render(doc[1], dpi=40)
        r2 = render(p, 1, dpi=40)
        r3 = render(doc.tobytes(), 1, dpi=40)
    assert diff_ratio(r1, r2) == 0.0 == diff_ratio(r2, r3)
    assert r1.width < r1.height  # /Rotate 180 keeps portrait orientation
