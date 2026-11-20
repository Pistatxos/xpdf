"""Utilidades compartidas por los tests de texto."""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf


@dataclass(frozen=True)
class FakeFont:
    css_family: str
    buffer: bytes
    filename: str
    original_name: str
    bold: bool
    italic: bool
    source: str = "bundled"

    @property
    def substituted(self) -> bool:
        return True


_BASE = {
    (False, False, False): "helv", (True, False, False): "hebo",
    (False, True, False): "heit", (True, True, False): "hebi",
    (False, False, True): "tiro", (True, False, True): "tibo",
    (False, True, True): "tiit", (True, True, True): "tibi",
}


class FakeResolver:
    """Resuelve a las fuentes base de MuPDF (Nimbus) según negrita/cursiva/serif."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def resolve(self, req: object, text: str) -> FakeFont:
        name: str = getattr(req, "name")
        flags: int = getattr(req, "flags")
        self.calls.append((name, text))
        low = name.lower()
        bold = bool(flags & 16) or "bold" in low or "hebo" in low or "tibo" in low
        italic = bool(flags & 2) or "italic" in low or "oblique" in low
        if "sans" in low or "helv" in low or "arial" in low:
            serif = False  # el nombre manda: las banderas de MuPDF fallan a menudo
        else:
            serif = bool(flags & 4) or any(k in low for k in ("times", "tiro", "roman", "serif"))
        code = _BASE[(bold, italic, serif)]
        return FakeFont(code, pymupdf.Font(code).buffer, f"{code}.ttf", name, bold, italic)

    def missing_glyphs(self, buffer: bytes, text: str) -> set[str]:
        font = pymupdf.Font(fontbuffer=buffer)
        return {c for c in text if c.strip() and not font.has_glyph(ord(c))}
