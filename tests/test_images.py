from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from xpdf.core.images import (
    ImageError,
    add_image,
    delete_image,
    image_at,
    list_images,
    move_image,
    replace_image,
)

RED, BLUE, BG = (200, 30, 30), (30, 60, 200), (230, 240, 255)


def _solid(color: tuple[int, int, int], w: int = 40, h: int = 20) -> pymupdf.Pixmap:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, color)
    return pix


def _px(doc: pymupdf.Document, x: float, y: float, page: int = 0) -> tuple[int, ...]:
    pix = doc[page].get_pixmap(dpi=72)
    return tuple(pix.pixel(int(x), int(y))[:3])


def _near(a: tuple[int, ...], b: tuple[int, ...], tol: int = 12) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


@pytest.fixture
def doc() -> pymupdf.Document:
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    p.insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=_solid(BG, 10, 10), keep_proportion=False, overlay=False)
    x = p.insert_image(pymupdf.Rect(400, 40, 560, 120), pixmap=_solid(RED))
    p.insert_image(pymupdf.Rect(50, 700, 130, 740), xref=x)  # el mismo logo, repetido
    p.insert_text((72, 300), "Texto encima del fondo", fontsize=14)
    return pymupdf.open("pdf", d.tobytes())


def test_list_and_hit(doc: pymupdf.Document) -> None:
    imgs = list_images(doc, 0)
    assert len(imgs) == 3 and all(i.blocked_reason is None for i in imgs)
    hit = image_at(doc, 0, 480, 80)
    assert hit is not None and hit.bbox == pytest.approx((400, 40, 560, 120), abs=0.5)
    # el fondo no «tapa» el clic cuando hay una imagen más pequeña encima
    assert image_at(doc, 0, 90, 720).bbox == pytest.approx((50, 700, 130, 740), abs=0.5)  # type: ignore[union-attr]


def test_delete_only_that_placement(doc: pymupdf.Document) -> None:
    logo = image_at(doc, 0, 480, 80)
    assert logo is not None
    delete_image(doc, 0, logo.number)
    assert _near(_px(doc, 480, 80), BG)  # se ve el fondo donde estaba
    assert _near(_px(doc, 90, 720), RED)  # el otro dibujo del mismo logo sigue
    assert len(list_images(doc, 0)) == 2
    assert "Texto encima del fondo" in doc[0].get_text()


def test_move_and_resize_keep_layer_order(doc: pymupdf.Document) -> None:
    bgimg = next(i for i in list_images(doc, 0) if i.bbox[2] - i.bbox[0] > 500)
    move_image(doc, 0, bgimg.number, (0, 0, 595, 842))  # sin cambio: sigue debajo del texto
    logo = image_at(doc, 0, 480, 80)
    assert logo is not None
    move_image(doc, 0, logo.number, (100, 200, 340, 320))  # mover y agrandar (x1,5)
    moved = image_at(doc, 0, 220, 260)
    assert moved is not None and moved.bbox == pytest.approx((100, 200, 340, 320), abs=0.5)
    assert _near(_px(doc, 220, 260), RED) and _near(_px(doc, 480, 80), BG)
    # el fondo sigue detrás del texto: el texto sigue extrayéndose y viéndose
    assert "Texto encima del fondo" in doc[0].get_text()
    assert _px(doc, 80, 296) != BG or _px(doc, 90, 296) != BG


def test_add_downscales_big_photos(doc: pymupdf.Document, tmp_path: Path) -> None:
    big = tmp_path / "foto.png"
    _solid(BLUE, 4000, 2000).save(str(big))
    box = add_image(doc, 0, (100, 400, 300, 500), big)
    assert box == pytest.approx((100, 400, 300, 500), abs=0.5)
    assert _near(_px(doc, 200, 450), BLUE)
    new = max(list_images(doc, 0), key=lambda i: i.number)
    # 200 pt de ancho a 300 ppp = ~833 px: no se incrustan 4000
    assert new.width <= 1300


def test_add_keeps_proportion(doc: pymupdf.Document, tmp_path: Path) -> None:
    img = tmp_path / "logo.png"
    _solid(BLUE, 200, 100).save(str(img))
    box = add_image(doc, 0, (100, 400, 300, 600), img)  # caja cuadrada, imagen 2:1
    assert box == pytest.approx((100, 450, 300, 550), abs=0.5)


def test_replace(doc: pymupdf.Document, tmp_path: Path) -> None:
    img = tmp_path / "nuevo.png"
    _solid(BLUE, 160, 80).save(str(img))
    logo = image_at(doc, 0, 480, 80)
    assert logo is not None
    replace_image(doc, 0, logo.number, img)
    assert _near(_px(doc, 480, 80), BLUE)
    assert _near(_px(doc, 90, 720), RED)  # la otra copia del logo no cambia


def test_rejects_unsupported_format(doc: pymupdf.Document, tmp_path: Path) -> None:
    bad = tmp_path / "x.gif"
    bad.write_bytes(b"GIF89a")
    with pytest.raises(ImageError):
        add_image(doc, 0, (0, 0, 10, 10), bad)


def test_image_inside_form_is_blocked(tmp_path: Path) -> None:
    src = pymupdf.open()
    sp = src.new_page(width=200, height=100)
    sp.insert_image(sp.rect, pixmap=_solid(RED))
    d = pymupdf.open()
    p = d.new_page()
    p.show_pdf_page(pymupdf.Rect(100, 100, 300, 200), src, 0)  # la imagen queda dentro de un grupo
    d = pymupdf.open("pdf", d.tobytes())
    imgs = list_images(d, 0)
    assert len(imgs) == 1 and imgs[0].blocked_reason is not None
    with pytest.raises(ImageError, match="grupo"):
        delete_image(d, 0, imgs[0].number)


def test_inline_image_can_be_deleted_and_moved() -> None:
    d = pymupdf.open()
    p = d.new_page(width=200, height=200)
    # imagen incrustada 2x1 roja en el contenido (BI ... ID ... EI)
    inline = b"q 100 0 0 50 20 120 cm BI /W 2 /H 1 /CS /RGB /BPC 8 ID \xc8\x1e\x1e\xc8\x1e\x1e EI Q"
    p.insert_text((10, 20), "hola", fontsize=10)  # crea el flujo y recursos
    xref = d.get_new_xref()
    d.update_object(xref, "<<>>")
    d.update_stream(xref, inline)
    d.xref_set_key(p.xref, "Contents", f"[{' '.join(f'{x} 0 R' for x in [*p.get_contents(), xref])}]")
    d = pymupdf.open("pdf", d.tobytes())
    imgs = list_images(d, 0)
    assert len(imgs) == 1 and imgs[0].blocked_reason is None
    move_image(d, 0, imgs[0].number, (20, 20, 120, 70))
    assert list_images(d, 0)[0].bbox == pytest.approx((20, 20, 120, 70), abs=0.5)
    delete_image(d, 0, list_images(d, 0)[0].number)
    assert list_images(d, 0) == [] and "hola" in d[0].get_text()


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_page(rotation: int, tmp_path: Path) -> None:
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    p.set_rotation(rotation)
    d = pymupdf.open("pdf", d.tobytes())
    img = tmp_path / "logo.png"
    _solid(RED, 200, 100).save(str(img))
    box = add_image(d, 0, (50, 50, 250, 150), img)  # visual
    info = list_images(d, 0)[0]
    assert info.bbox == pytest.approx(box, abs=0.6)
    pix = d[0].get_pixmap(dpi=72)
    assert _near(tuple(pix.pixel(150, 100)[:3]), RED)
    move_image(d, 0, info.number, (300, 300, 400, 350))
    assert list_images(d, 0)[0].bbox == pytest.approx((300, 300, 400, 350), abs=0.6)


def test_commands_undo_redo(doc: pymupdf.Document, tmp_path: Path) -> None:
    from xpdf.core.document import Document
    from xpdf.core.history import History
    from xpdf.core.images import add_command, delete_command, images_on, move_command

    path = tmp_path / "in.pdf"
    doc.save(path)
    d = Document.open(path)
    h = History()
    logo = next(i for i in images_on(d, 0) if i.bbox[0] > 300)
    h.push(move_command(d, 0, logo.number, (10, 10, 170, 90)))
    h.push(delete_command(d, 0, logo.number))
    img = tmp_path / "n.png"
    _solid(BLUE).save(str(img))
    h.push(add_command(d, 0, (300, 300, 380, 340), img))
    assert len(images_on(d, 0)) == 3
    h.undo()
    h.undo()
    assert any(i.bbox == pytest.approx((10, 10, 170, 90), abs=0.5) for i in images_on(d, 0))
    h.undo()
    assert any(i.bbox == pytest.approx((400, 40, 560, 120), abs=0.5) for i in images_on(d, 0))
