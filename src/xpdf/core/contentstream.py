"""Lector mínimo de flujos de contenido PDF para localizar dibujos de imágenes.

PyMuPDF dice *dónde* está cada imagen (`page.get_image_info`), pero no qué
instrucción del contenido la dibuja. Para borrar o mover una imagen concreta sin
tocar las demás (ni el mismo logo en otras posiciones) hace falta encontrar esa
instrucción: `/Nombre Do` para imágenes en XObject, o `BI … ID … EI` para
imágenes incrustadas.

Este módulo recorre los flujos de contenido de la página en orden, siguiendo la
matriz de transformación (CTM) con `q`/`Q`/`cm`, y devuelve cada dibujo de
imagen con su posición en bytes y su matriz. No interpreta nada más.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pymupdf

_WS = b" \t\r\n\f\x00"
_DELIM = b"()<>[]{}/%"


@dataclass(frozen=True, slots=True)
class Token:
    kind: str  # "num", "name", "op", "str", "hex", "array", "dict", "inline"
    value: bytes
    start: int
    end: int


def tokenize(data: bytes) -> Iterator[Token]:
    """Tokens de un flujo de contenido (los arrays y diccionarios van enteros)."""
    i, n = 0, len(data)
    while i < n:
        c = data[i : i + 1]
        if c in (b"",):
            break
        if c[0] in _WS:
            i += 1
            continue
        if c == b"%":  # comentario hasta fin de línea
            while i < n and data[i] not in b"\r\n":
                i += 1
            continue
        start = i
        if c == b"(":
            i = _skip_string(data, i)
            yield Token("str", data[start:i], start, i)
        elif c == b"<" and data[i : i + 2] == b"<<":
            i = _skip_balanced(data, i, b"<<", b">>")
            yield Token("dict", data[start:i], start, i)
        elif c == b"<":
            j = data.find(b">", i)
            i = n if j < 0 else j + 1
            yield Token("hex", data[start:i], start, i)
        elif c == b"[":
            i = _skip_array(data, i)
            yield Token("array", data[start:i], start, i)
        elif c == b"/":
            i += 1
            while i < n and data[i] not in _WS and data[i] not in _DELIM:
                i += 1
            yield Token("name", data[start:i], start, i)
        else:
            while i < n and data[i] not in _WS and data[i] not in _DELIM:
                i += 1
            if i == start:  # delimitador suelto (p. ej. «]» o «)» huérfanos)
                i += 1
            word = data[start:i]
            if word == b"BI":
                # Imagen incrustada: BI <dict> ID <binario> EI
                j = data.find(b"ID", i)
                k = _find_ei(data, j + 2) if j >= 0 else -1
                end = n if k < 0 else k + 2
                yield Token("inline", data[start:end], start, end)
                i = end
                continue
            kind = "num" if _is_number(word) else "op"
            yield Token(kind, word, start, i)


def _is_number(word: bytes) -> bool:
    try:
        float(word)
    except ValueError:
        return False
    return True


def _skip_string(data: bytes, i: int) -> int:
    depth, i, n = 0, i, len(data)
    while i < n:
        ch = data[i]
        if ch == 0x5C:  # barra invertida: escapa el siguiente
            i += 2
            continue
        if ch == 0x28:
            depth += 1
        elif ch == 0x29:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _skip_balanced(data: bytes, i: int, open_: bytes, close: bytes) -> int:
    depth, n = 0, len(data)
    while i < n:
        if data.startswith(open_, i):
            depth += 1
            i += len(open_)
        elif data.startswith(close, i):
            depth -= 1
            i += len(close)
            if depth == 0:
                return i
        elif data[i] == 0x28:
            i = _skip_string(data, i)
        else:
            i += 1
    return n


def _skip_array(data: bytes, i: int) -> int:
    depth, n = 0, len(data)
    while i < n:
        ch = data[i]
        if ch == 0x28:
            i = _skip_string(data, i)
            continue
        if ch == 0x5B:
            depth += 1
        elif ch == 0x5D:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _find_ei(data: bytes, i: int) -> int:
    """Posición de «EI» que cierra una imagen incrustada (rodeado de espacios)."""
    n = len(data)
    while True:
        j = data.find(b"EI", i)
        if j < 0:
            return -1
        before = data[j - 1] if j > 0 else 0x20
        after = data[j + 2] if j + 2 < n else 0x20
        if before in _WS and (after in _WS or j + 2 >= n):
            return j
        i = j + 2


@dataclass(frozen=True, slots=True)
class ImageDraw:
    """Un dibujo de imagen en el contenido de la página."""

    stream_xref: int  # flujo de contenido que lo contiene
    start: int  # bytes [start, end) de «/Nombre Do» o del bloque BI…EI
    end: int
    ctm: pymupdf.Matrix  # CTM en el momento del dibujo (espacio PDF)
    name: str | None  # nombre del XObject; None si es incrustada
    is_form: bool  # True si el XObject es un formulario (grupo), no una imagen


def image_draws(page: pymupdf.Page) -> list[ImageDraw]:
    """Dibujos de imágenes y grupos de primer nivel de la página, en orden."""
    doc = page.parent
    xobjects = _xobject_kinds(page)
    ctm = pymupdf.Matrix(1, 0, 0, 1, 0, 0)
    stack: list[pymupdf.Matrix] = []
    operands: list[Token] = []
    out: list[ImageDraw] = []
    for xref in page.get_contents():
        data = doc.xref_stream(xref) or b""
        for tok in tokenize(data):
            if tok.kind != "op" and tok.kind != "inline":
                operands.append(tok)
                continue
            op = tok.value
            if tok.kind == "inline":
                out.append(ImageDraw(xref, tok.start, tok.end, pymupdf.Matrix(ctm), None, False))
            elif op == b"q":
                stack.append(pymupdf.Matrix(ctm))
            elif op == b"Q":
                if stack:
                    ctm = stack.pop()
            elif op == b"cm" and len(operands) >= 6:
                try:
                    a, b, c, d, e, f = (float(t.value) for t in operands[-6:])
                except ValueError:
                    pass
                else:
                    ctm = pymupdf.Matrix(a, b, c, d, e, f) * ctm
            elif op == b"Do" and operands and operands[-1].kind == "name":
                name_tok = operands[-1]
                name = name_tok.value[1:].decode("latin-1")
                kind = xobjects.get(name)
                if kind in ("Image", "Form"):
                    out.append(
                        ImageDraw(xref, name_tok.start, tok.end, pymupdf.Matrix(ctm), name, kind == "Form")
                    )
            operands = []
    return out


def _xobject_kinds(page: pymupdf.Page) -> dict[str, str]:
    """Nombre de recurso -> «Image» o «Form»."""
    out: dict[str, str] = {}
    for item in page.get_xobjects():  # (xref, name, invoker, bbox)
        out[str(item[1])] = "Form"
    for item in page.get_images(full=True):  # (xref, smask, w, h, bpc, cs, alt, name, filter, referencer)
        out[str(item[7])] = "Image"
    return out


def replace_span(page: pymupdf.Page, draw: ImageDraw, new: bytes) -> None:
    """Sustituye los bytes del dibujo por `new` en su flujo de contenido."""
    doc = page.parent
    data = doc.xref_stream(draw.stream_xref) or b""
    doc.update_stream(draw.stream_xref, data[: draw.start] + new + data[draw.end :])
