"""Operaciones de páginas: borrar, rotar y reordenar.

Nota: no se usa `Document.select()` para reordenar ni borrar: en PyMuPDF 1.28
elimina el diccionario /AcroForm y el PDF deja de ser un formulario. Se usan
`delete_pages`, `move_page` y `set_rotation`, que lo conservan (hay tests).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from xpdf.core.commands import SnapshotCommand
from xpdf.core.document import Document
from xpdf.core.errors import XpdfError


class PageError(XpdfError):
    """Operación de páginas no válida, con mensaje para el usuario."""


def _clean(doc: Document, pages: Iterable[int]) -> list[int]:
    out = sorted(set(pages))
    if not out:
        raise PageError("No hay páginas seleccionadas.")
    if out[0] < 0 or out[-1] >= doc.page_count:
        raise PageError("La selección incluye páginas que no existen.")
    return out


# ------------------------------------------------------------------ acciones
def delete_pages(doc: Document, pages: Iterable[int]) -> None:
    sel = _clean(doc, pages)
    if len(sel) >= doc.page_count:
        raise PageError("No se pueden borrar todas las páginas: el PDF debe tener al menos una.")
    doc.raw.delete_pages(sel)


def rotate_pages(doc: Document, pages: Iterable[int], degrees: int) -> None:
    """Gira las páginas `degrees` (múltiplo de 90; positivo = horario)."""
    if degrees % 90:
        raise PageError("Solo se puede girar en múltiplos de 90°.")
    for i in _clean(doc, pages):
        page = doc.raw[i]
        page.set_rotation((page.rotation + degrees) % 360)


def new_order(count: int, pages: Sequence[int], target: int) -> list[int]:
    """Orden resultante de mover `pages` (en su orden relativo) delante de `target`.

    `target` es una posición del orden actual, entre 0 y `count` (al final).
    """
    sel = sorted(set(pages))
    rest = [p for p in range(count) if p not in sel]
    # posición de inserción en `rest`: cuántas no seleccionadas quedan antes de target
    insert_at = sum(1 for p in rest if p < target)
    return rest[:insert_at] + sel + rest[insert_at:]


def move_pages(doc: Document, pages: Iterable[int], target: int) -> list[int]:
    """Mueve las páginas delante de la posición `target`. Devuelve sus nuevos índices."""
    sel = _clean(doc, pages)
    n = doc.page_count
    if not 0 <= target <= n:
        raise PageError("Posición de destino no válida.")
    order = new_order(n, sel, target)
    current = list(range(n))
    for i, wanted in enumerate(order):
        j = current.index(wanted)
        if j != i:
            doc.raw.move_page(j, i)  # coloca la página j delante de la que está en i
            current.insert(i, current.pop(j))
    return [order.index(p) for p in sel]


# ------------------------------------------------------------------ comandos
def _n(pages: Sequence[int]) -> str:
    return "página" if len(pages) == 1 else "páginas"


def delete_command(doc: Document, pages: Sequence[int]) -> SnapshotCommand[None]:
    sel = _clean(doc, pages)
    return SnapshotCommand(doc, f"borrar {_n(sel)}", lambda: delete_pages(doc, sel))


def rotate_command(doc: Document, pages: Sequence[int], degrees: int) -> SnapshotCommand[None]:
    sel = _clean(doc, pages)
    return SnapshotCommand(doc, f"girar {_n(sel)}", lambda: rotate_pages(doc, sel, degrees))


def move_command(doc: Document, pages: Sequence[int], target: int) -> SnapshotCommand[list[int]]:
    sel = _clean(doc, pages)
    return SnapshotCommand(doc, f"mover {_n(sel)}", lambda: move_pages(doc, sel, target))
