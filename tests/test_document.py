from __future__ import annotations

import os
import sys
from pathlib import Path

import pymupdf
import pytest

from xpdf.core.document import Document
from xpdf.core.errors import OpenError, SaveError


def test_open_basic(sample_pdf: Path) -> None:
    doc = Document.open(sample_pdf)
    assert doc.page_count == 3
    assert doc.path == sample_pdf
    s0 = doc.page_size(0)
    assert (round(s0.width), round(s0.height)) == (595, 842)
    assert doc.page_size(1).width > doc.page_size(1).height


def test_rotated_page_size_is_swapped(sample_pdf: Path) -> None:
    doc = Document.open(sample_pdf)
    assert doc.page_rotation(2) == 90
    s = doc.page_size(2)
    assert (round(s.width), round(s.height)) == (792, 612)


def test_render_dimensions(sample_pdf: Path) -> None:
    doc = Document.open(sample_pdf)
    r = doc.render_page(0, 2.0)
    assert (r.width, r.height) == (1190, 1684)
    assert r.stride >= r.width * 3
    assert len(r.samples) == r.stride * r.height
    # La página tiene texto: no todos los píxeles son blancos.
    assert set(r.samples) != {255}


def test_render_rejects_bad_scale(sample_pdf: Path) -> None:
    doc = Document.open(sample_pdf)
    with pytest.raises(ValueError):
        doc.render_page(0, 0)


def test_page_index_out_of_range(sample_pdf: Path) -> None:
    doc = Document.open(sample_pdf)
    with pytest.raises(IndexError):
        doc.page_size(3)


def test_open_missing(tmp_path: Path) -> None:
    with pytest.raises(OpenError, match="No se encuentra"):
        Document.open(tmp_path / "nope.pdf")


def test_open_garbage(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"esto no es un pdf")
    with pytest.raises(OpenError, match="no es un PDF"):
        Document.open(bad)


def test_open_encrypted(tmp_path: Path) -> None:
    d = pymupdf.open()
    d.new_page()
    enc = tmp_path / "enc.pdf"
    d.save(enc, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    with pytest.raises(OpenError, match="contraseña"):
        Document.open(enc)


def test_save_as_and_overwrite_original(sample_pdf: Path, tmp_path: Path) -> None:
    doc = Document.open(sample_pdf)
    out = tmp_path / "copia.pdf"
    doc.save_as(out)
    assert doc.path == out
    assert Document.open(out).page_count == 3
    # Sobrescribir el archivo actualmente abierto debe funcionar.
    doc.save()
    assert Document.open(out).page_count == 3
    # El documento sigue siendo usable tras guardar.
    assert doc.render_page(0, 0.5).width > 0
    assert not any(p.name.startswith(".xpdf-") for p in tmp_path.iterdir())


def test_save_without_path() -> None:
    doc = Document.new_blank()
    with pytest.raises(SaveError):
        doc.save()


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="permisos POSIX")
def test_save_to_readonly_dir_keeps_document(sample_pdf: Path, tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        doc = Document.open(sample_pdf)
        with pytest.raises(SaveError):
            doc.save_as(ro / "x.pdf")
        assert doc.page_count == 3
        assert doc.path == sample_pdf
    finally:
        ro.chmod(0o700)
