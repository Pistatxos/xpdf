from __future__ import annotations

import os
from pathlib import Path

import pymupdf
import pytest

from xpdf.core.document import Document
from xpdf.core.errors import SaveError
from xpdf.core.optimize import (
    Level,
    OptimizeCancelled,
    OptimizeError,
    OptimizeResult,
    format_result,
    format_size,
    optimize,
    snapshot,
)


# ------------------------------------------------------------------ helpers
def _picture(width: int, height: int, cs: pymupdf.Colorspace = pymupdf.csRGB) -> pymupdf.Pixmap:
    """Imagen «fotográfica» de alta resolución: formas de colores rasterizadas."""
    src = pymupdf.open()
    page = src.new_page(width=400, height=300)
    for i in range(12):
        r = pymupdf.Rect(i * 25, i * 15, i * 25 + 160, i * 15 + 120)
        page.draw_oval(r, color=None, fill=((i * 37 % 255) / 255, (i * 91 % 255) / 255, 0.5))
    page.insert_text((40, 260), "Texto dentro de la imagen", fontsize=20)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(width / 400, height / 300), colorspace=cs)
    src.close()
    return pix


def _render(doc: pymupdf.Document, index: int, scale: float = 0.5) -> bytes:
    return bytes(doc[index].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).samples)


def _mean_abs_diff(a: bytes, b: bytes) -> float:
    assert len(a) == len(b)
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


@pytest.fixture
def heavy_pdf(tmp_path: Path) -> Path:
    """3 páginas: PNG grande (sin pérdida), JPEG q95 grande y una imagen CMYK."""
    doc = pymupdf.open()
    rgb = _picture(2400, 1800)
    p = doc.new_page()
    p.insert_text((72, 60), "Página con PNG — áéíóú ñ", fontsize=14)
    p.insert_image(pymupdf.Rect(72, 80, 472, 380), stream=rgb.tobytes("png"))
    p = doc.new_page()
    p.insert_text((72, 60), "Página con JPEG", fontsize=14)
    p.insert_image(pymupdf.Rect(72, 80, 472, 380), stream=rgb.tobytes("jpg", jpg_quality=95))
    p = doc.new_page(width=842, height=595)
    p.insert_text((72, 60), "Página con CMYK", fontsize=14)
    cmyk = pymupdf.Pixmap(pymupdf.csCMYK, _picture(1600, 1200))
    p.insert_image(pymupdf.Rect(72, 80, 472, 380), stream=cmyk.tobytes("jpg", jpg_quality=95))
    path = tmp_path / "heavy.pdf"
    doc.save(path)
    doc.close()
    return path


# -------------------------------------------------------------------- tests
@pytest.mark.parametrize("level", list(Level))
def test_levels_preserve_content(heavy_pdf: Path, tmp_path: Path, level: Level) -> None:
    out = tmp_path / f"{level.value}.pdf"
    result = optimize(Document.open(heavy_pdf), out, level)

    original_size = heavy_pdf.stat().st_size
    assert result.original_bytes == original_size
    assert result.new_bytes == out.stat().st_size <= original_size

    src, dst = pymupdf.open(heavy_pdf), pymupdf.open(out)
    assert dst.page_count == src.page_count
    for i in range(src.page_count):
        assert dst[i].rect == src[i].rect
        assert dst[i].get_text() == src[i].get_text()
        diff = _mean_abs_diff(_render(src, i), _render(dst, i))
        assert diff < (0.01 if level is Level.LOSSLESS else 4.0), (i, diff)


def test_lossy_levels_shrink_a_lot(heavy_pdf: Path, tmp_path: Path) -> None:
    doc = Document.open(heavy_pdf)
    balanced = optimize(doc, tmp_path / "b.pdf", Level.BALANCED)
    small = optimize(doc, tmp_path / "s.pdf", Level.SMALL)
    assert balanced.reduced and balanced.images_recompressed >= 2
    assert balanced.ratio < 0.5
    assert small.new_bytes <= balanced.new_bytes
    # No se amplía nada: las imágenes resultantes no superan el tamaño original.
    out = pymupdf.open(tmp_path / "s.pdf")
    for i in range(out.page_count):
        for img in out[i].get_images(full=True):
            assert img[2] <= 2400 and img[3] <= 1800


def test_cmyk_colors_preserved(heavy_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "s.pdf"
    optimize(Document.open(heavy_pdf), out, Level.SMALL)
    clip = pymupdf.Rect(72, 80, 472, 380)
    a = pymupdf.open(heavy_pdf)[2].get_pixmap(clip=clip, dpi=36).samples
    b = pymupdf.open(out)[2].get_pixmap(clip=clip, dpi=36).samples
    assert _mean_abs_diff(bytes(a), bytes(b)) < 4.0  # p. ej. sin inversión de colores


def test_never_larger_keeps_original(sample_pdf: Path, tmp_path: Path) -> None:
    # Un PDF ya optimizado: volver a optimizarlo no puede ganar nada.
    first = tmp_path / "first.pdf"
    optimize(Document.open(sample_pdf), first, Level.LOSSLESS)
    second = tmp_path / "second.pdf"
    result = optimize(Document.open(first), second, Level.SMALL)
    assert result.new_bytes <= result.original_bytes
    if not result.reduced:
        assert second.read_bytes() == first.read_bytes()
        assert result.images_recompressed == 0


def test_source_document_untouched(heavy_pdf: Path, tmp_path: Path) -> None:
    original = heavy_pdf.read_bytes()
    doc = Document.open(heavy_pdf)
    xrefs = [img[0] for img in doc.raw[0].get_images(full=True)]
    before_streams = [doc.raw.xref_stream_raw(x) for x in xrefs]
    before_render = doc.render_page(0, 0.3).samples
    optimize(doc, tmp_path / "out.pdf", Level.SMALL)
    assert [doc.raw.xref_stream_raw(x) for x in xrefs] == before_streams
    assert doc.render_page(0, 0.3).samples == before_render
    assert not doc.raw.is_dirty
    assert doc.path == heavy_pdf
    assert heavy_pdf.read_bytes() == original


def test_unsaved_changes_are_included(sample_pdf: Path, tmp_path: Path) -> None:
    doc = Document.open(sample_pdf)
    doc.raw[0].insert_text((72, 200), "Cambio sin guardar", fontsize=12)
    out = tmp_path / "out.pdf"
    optimize(doc, out, Level.BALANCED)
    assert "Cambio sin guardar" in pymupdf.open(out)[0].get_text()


def test_atomic_on_error(
    heavy_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out.pdf"
    out.write_bytes(b"previo")

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("disco lleno")

    monkeypatch.setattr(pymupdf.Document, "save", boom)
    with pytest.raises(OptimizeError):
        optimize(Document.open(heavy_pdf), out, Level.BALANCED)
    assert out.read_bytes() == b"previo"
    assert not any(p.name.startswith(".xpdf-") for p in tmp_path.iterdir())


def test_cancel_leaves_no_file(heavy_pdf: Path, tmp_path: Path) -> None:
    calls: list[float] = []

    def progress(f: float) -> None:
        calls.append(f)
        if f > 0.05:
            raise OptimizeCancelled()

    out = tmp_path / "out.pdf"
    with pytest.raises(OptimizeCancelled):
        optimize(Document.open(heavy_pdf), out, Level.BALANCED, progress)
    assert not out.exists()
    assert not any(p.name.startswith(".xpdf-") for p in tmp_path.iterdir())


def test_progress_reaches_one(heavy_pdf: Path, tmp_path: Path) -> None:
    calls: list[float] = []
    optimize(Document.open(heavy_pdf), tmp_path / "o.pdf", Level.SMALL, calls.append)
    assert calls[0] == 0.0 and calls[-1] == 1.0
    assert calls == sorted(calls)


def test_refuses_to_overwrite_original(sample_pdf: Path) -> None:
    with pytest.raises(OptimizeError, match="distinto"):
        optimize(Document.open(sample_pdf), sample_pdf, Level.LOSSLESS)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="permisos POSIX")
def test_readonly_dir(sample_pdf: Path, tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        with pytest.raises(SaveError):
            optimize(Document.open(sample_pdf), ro / "x.pdf", Level.LOSSLESS)
    finally:
        ro.chmod(0o700)


def test_forms_survive(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    w = pymupdf.Widget()
    w.field_name = "nombre"
    w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    w.rect = pymupdf.Rect(72, 72, 300, 100)
    w.field_value = "Juan"
    page.add_widget(w)
    src = tmp_path / "form.pdf"
    doc.save(src)

    d = Document.open(src)
    snap = snapshot(d)
    assert snap.has_forms and not snap.has_signatures
    out = tmp_path / "out.pdf"
    result = optimize(d, out, Level.SMALL)
    assert not result.fonts_subset  # con formularios no se subconjuntan fuentes
    widgets = list(pymupdf.open(out)[0].widgets())
    assert [(x.field_name, x.field_value) for x in widgets] == [("nombre", "Juan")]

    # El guardado normal (sin pérdida) también los conserva.
    d.save_as(tmp_path / "saved.pdf")
    widgets = list(pymupdf.open(tmp_path / "saved.pdf")[0].widgets())
    assert [(x.field_name, x.field_value) for x in widgets] == [("nombre", "Juan")]


def test_save_as_does_not_bloat(heavy_pdf: Path, tmp_path: Path) -> None:
    doc = Document.open(heavy_pdf)
    out = tmp_path / "saved.pdf"
    doc.save_as(out)
    assert out.stat().st_size <= heavy_pdf.stat().st_size
    size = out.stat().st_size
    doc.save()  # guardar otra vez lo ya guardado no crece
    assert out.stat().st_size <= size


def test_format() -> None:
    assert format_size(512) == "512 B"
    assert format_size(int(12.4 * 1024 * 1024)) == "12,4 MB"
    r = OptimizeResult(
        int(12.4 * 1024 * 1024), int(3.1 * 1024 * 1024), True, 3, True
    )
    assert format_result(r) == "12,4 MB → 3,1 MB (−75 %)"
    same = OptimizeResult(1000, 1000, False, 0, False)
    assert format_result(same).endswith("(0 %)")


def test_dialog_smoke(heavy_pdf: Path, tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # ImportError incluye la falta de librerías del sistema (p. ej. libGL en Linux).
    widgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    from xpdf.ui.optimize_dialog import OptimizeDialog

    app = widgets.QApplication.instance() or widgets.QApplication([])
    assert app is not None
    dialog = OptimizeDialog(None, Document.open(heavy_pdf))
    out = tmp_path / "heavy (reducido).pdf"
    result = dialog.optimize_to(out)
    assert result is not None and result.reduced
    assert out.is_file()
    dialog.deleteLater()
