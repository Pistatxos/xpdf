"""Deshacer / rehacer con el patrón comando.

Cada operación que modifica el documento se implementa como un `Command` con
`do()` y `undo()`. `History` mantiene las pilas y notifica cambios.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable


class Command(ABC):
    """Operación reversible sobre el documento."""

    #: Texto corto para el menú («Deshacer <label>»).
    label: str = ""

    def cost(self) -> int:
        """Memoria aproximada que retiene el comando (bytes)."""
        return 0

    @abstractmethod
    def do(self) -> None: ...

    @abstractmethod
    def undo(self) -> None: ...


class History:
    def __init__(self, limit: int = 200, max_bytes: int = 512 * 1024 * 1024) -> None:
        self._undo: list[Command] = []
        self._redo: list[Command] = []
        self._limit = limit
        self._max_bytes = max_bytes
        self._clean_index: int | None = 0
        self._listeners: list[Callable[[], None]] = []

    def subscribe(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in self._listeners:
            fn()

    def push(self, cmd: Command) -> None:
        """Ejecuta `cmd` y lo apila."""
        cmd.do()
        if self._clean_index is not None and self._clean_index > len(self._undo):
            self._clean_index = None  # el estado guardado estaba en la rama de rehacer
        self._undo.append(cmd)
        self._redo.clear()
        # Se descartan los pasos más antiguos si se superan los límites (siempre
        # se conserva al menos el último).
        while len(self._undo) > 1 and (
            len(self._undo) > self._limit
            or sum(c.cost() for c in self._undo) > self._max_bytes
        ):
            self._undo.pop(0)
            if self._clean_index is not None:
                self._clean_index -= 1
                if self._clean_index < 0:
                    self._clean_index = None
        self._notify()

    def undo(self) -> None:
        if not self._undo:
            return
        cmd = self._undo.pop()
        cmd.undo()
        self._redo.append(cmd)
        self._notify()

    def redo(self) -> None:
        if not self._redo:
            return
        cmd = self._redo.pop()
        cmd.do()
        self._undo.append(cmd)
        self._notify()

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._clean_index = 0
        self._notify()

    def mark_clean(self) -> None:
        """Marca el estado actual como guardado."""
        self._clean_index = len(self._undo)
        self._notify()

    @property
    def is_clean(self) -> bool:
        return self._clean_index == len(self._undo)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str:
        return self._undo[-1].label if self._undo else ""

    @property
    def redo_label(self) -> str:
        return self._redo[-1].label if self._redo else ""
