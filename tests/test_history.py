from __future__ import annotations

from xpdf.core.history import Command, History


class Append(Command):
    def __init__(self, target: list[int], value: int) -> None:
        self.target = target
        self.value = value
        self.label = f"añadir {value}"

    def do(self) -> None:
        self.target.append(self.value)

    def undo(self) -> None:
        self.target.pop()


def test_undo_redo() -> None:
    data: list[int] = []
    h = History()
    h.push(Append(data, 1))
    h.push(Append(data, 2))
    assert data == [1, 2]
    h.undo()
    assert data == [1] and h.can_redo and h.redo_label == "añadir 2"
    h.redo()
    assert data == [1, 2] and not h.can_redo
    h.undo(); h.undo(); h.undo()  # de más: no falla
    assert data == [] and not h.can_undo


def test_push_clears_redo() -> None:
    data: list[int] = []
    h = History()
    h.push(Append(data, 1))
    h.undo()
    h.push(Append(data, 3))
    assert not h.can_redo and data == [3]


def test_clean_state_tracking() -> None:
    data: list[int] = []
    h = History()
    assert h.is_clean
    h.push(Append(data, 1))
    assert not h.is_clean
    h.mark_clean()
    assert h.is_clean
    h.push(Append(data, 2))
    assert not h.is_clean
    h.undo()
    assert h.is_clean  # volver al estado guardado
    h.undo()
    assert not h.is_clean
    h.push(Append(data, 9))  # rama nueva: el estado guardado ya no es alcanzable
    assert not h.is_clean
    h.undo()
    assert not h.is_clean  # [] no es el estado guardado ([1])


def test_limit_drops_oldest() -> None:
    data: list[int] = []
    h = History(limit=3)
    for i in range(5):
        h.push(Append(data, i))
    for _ in range(5):
        h.undo()
    assert data == [0, 1]
    assert not h.is_clean


def test_listener_called() -> None:
    calls: list[int] = []
    h = History()
    h.subscribe(lambda: calls.append(1))
    h.push(Append([], 1))
    h.undo()
    h.redo()
    assert len(calls) == 3
