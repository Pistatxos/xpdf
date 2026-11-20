from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from xpdf.core.document import Document
from xpdf.core.history import History
from xpdf.core.pages import (
    PageError,
    delete_command,
    move_command,
    new_order,
    rotate_command,
)

SAMPLES = Path(__file__).parent / "samples"


def _labels(doc: Document) -> list[str]:
    return [str(doc.raw[i].get_text()).strip().split()[0] for i in range(doc.page_count)]


@pytest.fixture
def five(tmp_path: Path) -> Document:
    d = pymupdf.open()
    for i in range(5):
        p = d.new_page(width=595, height=842)
        p.insert_text((72, 72), f"P{i}", fontsize=20)
    d.set_toc([[1, f"Capítulo {i}", i + 1] for i in range(5)])
    # enlace de la página 0 a la 3
    d[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(72, 100, 200, 120), "page": 3})
    path = tmp_path / "five.pdf"
    d.save(path)
    return Document.open(path)


@pytest.mark.parametrize(
    ("pages", "target", "expected"),
    [
        ([3], 0, [3, 0, 1, 2, 4]),
        ([0], 5, [1, 2, 3, 4, 0]),
        ([1, 3], 0, [1, 3, 0, 2, 4]),
        ([0, 4], 2, [1, 0, 4, 2, 3]),
        ([2], 2, [0, 1, 2, 3, 4]),
        ([2], 3, [0, 1, 2, 3, 4]),
        ([1, 2], 4, [0, 3, 1, 2, 4]),
    ],
)
def test_new_order(pages: list[int], target: int, expected: list[int]) -> None:
    assert new_order(5, pages, target) == expected


def test_move_undo_redo(five: Document) -> None:
    h = History()
    cmd = move_command(five, [1, 3], 0)
    h.push(cmd)
    assert _labels(five) == ["P1", "P3", "P0", "P2", "P4"]
    assert cmd.result == [0, 1]
    h.undo()
    assert _labels(five) == ["P0", "P1", "P2", "P3", "P4"]
    h.redo()
    assert _labels(five) == ["P1", "P3", "P0", "P2", "P4"]


def test_move_keeps_toc_and_links_pointing_to_same_pages(five: Document) -> None:
    History().push(move_command(five, [3], 0))  # P3 pasa a ser la primera
    toc = five.raw.get_toc()
    by_title = {t[1]: t[2] for t in toc}
    assert by_title["Capítulo 3"] == 1 and by_title["Capítulo 0"] == 2
    # el enlace de P0 (ahora página 2) sigue apuntando a P3 (ahora página 1)
    links = five.raw[1].get_links()
    assert links and links[0]["page"] == 0


def test_delete(five: Document) -> None:
    h = History()
    h.push(delete_command(five, [0, 2]))
    assert _labels(five) == ["P1", "P3", "P4"]
    h.undo()
    assert five.page_count == 5


def test_cannot_delete_all(five: Document) -> None:
    with pytest.raises(PageError, match="al menos una"):
        History().push(delete_command(five, range(5)))
    assert five.page_count == 5


def test_rotate(five: Document) -> None:
    h = History()
    h.push(rotate_command(five, [1, 2], 90))
    assert [five.page_rotation(i) for i in range(5)] == [0, 90, 90, 0, 0]
    assert five.page_size(1).width > five.page_size(1).height
    h.push(rotate_command(five, [1], -90))
    h.push(rotate_command(five, [2], 270))
    assert [five.page_rotation(i) for i in range(5)] == [0, 0, 0, 0, 0]
    with pytest.raises(PageError):
        rotate_command(five, [0], 45).do()


def test_operations_keep_form_fields(tmp_path: Path) -> None:
    d = pymupdf.open(SAMPLES / "form_irs_w9.pdf")
    d.fullcopy_page(0)
    d.fullcopy_page(0)
    path = tmp_path / "form3.pdf"
    d.save(path)
    doc = Document.open(path)
    h = History()
    h.push(move_command(doc, [2], 0))
    h.push(rotate_command(doc, [0], 90))
    h.push(delete_command(doc, [1]))
    out = tmp_path / "out.pdf"
    doc.save_as(out)
    saved = pymupdf.open(out)
    assert saved.is_form_pdf
    assert saved.page_count == 2
    assert sum(len(list(p.widgets())) for p in saved) == 46


def test_invalid_selection(five: Document) -> None:
    with pytest.raises(PageError):
        delete_command(five, [])
    with pytest.raises(PageError):
        move_command(five, [9], 0)
