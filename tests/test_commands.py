from __future__ import annotations

import pytest

from xpdf.core.commands import SnapshotCommand
from xpdf.core.document import Document
from xpdf.core.history import History


def _text(doc: Document) -> str:
    return str(doc.raw[0].get_text()).strip()


def _add(doc: Document, text: str, y: float) -> SnapshotCommand[None]:
    return SnapshotCommand(doc, "añadir", lambda: doc.raw[0].insert_text((72, y), text), (0,))


def test_snapshot_undo_redo_roundtrip() -> None:
    doc = Document.new_blank()
    h = History()
    v0 = doc.version
    h.push(_add(doc, "uno", 100))
    h.push(_add(doc, "dos", 120))
    assert _text(doc) == "uno\ndos" and doc.version > v0
    h.undo()
    assert _text(doc) == "uno"
    h.undo()
    assert _text(doc) == ""
    h.redo()
    h.redo()
    assert _text(doc) == "uno\ndos"
    h.undo()
    h.push(_add(doc, "tres", 140))
    assert _text(doc) == "uno\ntres" and not h.can_redo


def test_failed_action_leaves_document_untouched() -> None:
    doc = Document.new_blank()
    h = History()
    h.push(_add(doc, "uno", 100))

    def boom() -> None:
        doc.raw[0].insert_text((72, 200), "a medias")
        raise RuntimeError("fallo")

    with pytest.raises(RuntimeError):
        h.push(SnapshotCommand(doc, "roto", boom, (0,)))
    assert _text(doc) == "uno"
    assert h.undo_label == "añadir"


def test_undo_survives_save(tmp_path) -> None:  # type: ignore[no-untyped-def]
    doc = Document.new_blank()
    h = History()
    h.push(_add(doc, "uno", 100))
    doc.save_as(tmp_path / "a.pdf")
    h.mark_clean()
    h.push(_add(doc, "dos", 120))
    h.undo()
    assert h.is_clean and _text(doc) == "uno"
    h.undo()
    assert _text(doc) == "" and not h.is_clean


def test_history_byte_budget_drops_oldest() -> None:
    doc = Document.new_blank()
    size = len(doc.snapshot())
    h = History(max_bytes=size * 3)
    for i in range(10):
        h.push(_add(doc, f"l{i}", 100 + 12 * i))
    n = 0
    while h.can_undo:
        h.undo()
        n += 1
    assert 1 <= n <= 4
