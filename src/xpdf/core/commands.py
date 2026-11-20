"""Comandos de edición reversibles basados en instantáneas del documento."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

from xpdf.core.document import Document
from xpdf.core.history import Command

T = TypeVar("T")


class SnapshotCommand(Command, Generic[T]):
    """Ejecuta `action` sobre el documento; deshacer restaura el estado anterior.

    Cada comando guarda una sola instantánea: la de «antes» mientras está en la
    pila de deshacer y la de «después» mientras está en la de rehacer. Rehacer
    no vuelve a ejecutar la acción: restaura exactamente el resultado.

    Si `action` falla, el documento se deja como estaba y la excepción sigue
    su curso (el comando no se apila).
    """

    def __init__(
        self,
        doc: Document,
        label: str,
        action: Callable[[], T],
        pages: tuple[int, ...] = (),
    ) -> None:
        self.doc = doc
        self.label = label
        self._action: Callable[[], T] | None = action
        #: Páginas afectadas (vacío = estructura del documento cambiada).
        self.pages = pages
        self.result: T | None = None
        self._before: bytes | None = None
        self._after: bytes | None = None

    def do(self) -> None:
        before = self.doc.snapshot()
        if self._action is not None:
            try:
                self.result = self._action()
            except BaseException:
                self.doc.restore(before)
                raise
            self._action = None  # no retener referencias innecesarias
            self.doc.touch()
        else:
            assert self._after is not None
            self.doc.restore(self._after)
            self._after = None
        self._before = before

    def undo(self) -> None:
        assert self._before is not None
        self._after = self.doc.snapshot()
        self.doc.restore(self._before)
        self._before = None

    def cost(self) -> int:
        return len(self._before or b"") + len(self._after or b"")
