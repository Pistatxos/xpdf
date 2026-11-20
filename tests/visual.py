"""Visual comparison helpers for text-editing tests.

Typical use::

    from tests.visual import render, diff_ratio, dump_if_requested

    before = render(doc, 0)
    ...edit the paragraph whose bbox is `bbox` (PDF points, get_text space)...
    after = render(doc, 0)
    assert diff_ratio(before, after, bbox, outside=True, margin=1.5) == 0.0
    assert diff_ratio(before, after, bbox) < 0.05
    dump_if_requested("my_test", before, after, bbox)

Regions are given in *unrotated page space* (the coordinates returned by
``page.get_text()``); `render` remembers the page's rotation and zoom so the
rect is mapped onto the rendered pixels correctly, also for /Rotate pages.

Set ``XPDF_VISUAL_OUT=/some/dir`` to make `dump_if_requested` (and the
``visual_dump`` fixture in conftest.py) write before | after | diff PNGs there.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

try:  # numpy ships with PyMuPDF >= 1.25 wheels; keep a slow fallback anyway.
    import numpy as np
except ImportError:  # pragma: no cover - exercised only without numpy
    np = None  # type: ignore[assignment]

ENV_OUT = "XPDF_VISUAL_OUT"


@dataclass(frozen=True)
class Rendered:
    """A rendered page plus the matrix mapping page coordinates to pixels."""

    pix: pymupdf.Pixmap
    matrix: pymupdf.Matrix  # unrotated page space -> pixel space

    @property
    def width(self) -> int:
        return int(self.pix.width)

    @property
    def height(self) -> int:
        return int(self.pix.height)

    def png(self) -> bytes:
        return bytes(self.pix.tobytes("png"))


Image = Rendered | pymupdf.Pixmap


def render(src: Any, page_index: int = 0, dpi: int = 110, *, annots: bool = True) -> Rendered:
    """Render a page to an RGB pixmap (no alpha).

    `src` may be a Document, a Page (page_index is then ignored), a path or PDF bytes.
    """
    close = False
    if isinstance(src, (str, Path)):
        src = pymupdf.open(src)
        close = True
    elif isinstance(src, (bytes, bytearray)):
        src = pymupdf.open(stream=bytes(src), filetype="pdf")
        close = True
    page = src if isinstance(src, pymupdf.Page) else src[page_index]
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB, alpha=False, annots=annots)
    matrix = page.rotation_matrix * pymupdf.Matrix(zoom, zoom)
    if close:
        src.close()
    return Rendered(pix, matrix)


def _parts(img: Image) -> tuple[pymupdf.Pixmap, pymupdf.Matrix]:
    if isinstance(img, Rendered):
        return img.pix, img.matrix
    z = (img.xres or 72) / 72.0
    return img, pymupdf.Matrix(z, z)


def _pixel_box(
    region: pymupdf.Rect | tuple[float, float, float, float],
    matrix: pymupdf.Matrix,
    width: int,
    height: int,
    margin: float,
) -> tuple[int, int, int, int]:
    r = pymupdf.Rect(region)
    if margin:
        r = r + (-margin, -margin, margin, margin)
    pr = r * matrix  # Rect * Matrix gives the enclosing rect of the transformed corners
    x0 = max(0, min(width, math.floor(pr.x0)))
    y0 = max(0, min(height, math.floor(pr.y0)))
    x1 = max(0, min(width, math.ceil(pr.x1)))
    y1 = max(0, min(height, math.ceil(pr.y1)))
    return x0, y0, x1, y1


def _arr(pix: pymupdf.Pixmap) -> Any:
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)
    return a[:, : pix.width * pix.n].reshape(pix.height, pix.width, pix.n)


def diff_mask(a: Image, b: Image, tol: int = 24) -> Any:
    """Boolean HxW numpy array: True where any channel differs by more than `tol`."""
    if np is None:
        raise RuntimeError("diff_mask needs numpy")
    pa, _ = _parts(a)
    pb, _ = _parts(b)
    _check_same(pa, pb)
    da = _arr(pa).astype(np.int16)
    db = _arr(pb).astype(np.int16)
    return (np.abs(da - db) > tol).any(axis=2)


def _check_same(pa: pymupdf.Pixmap, pb: pymupdf.Pixmap) -> None:
    if (pa.width, pa.height, pa.n) != (pb.width, pb.height, pb.n):
        raise ValueError(f"image size mismatch: {pa.width}x{pa.height}x{pa.n} vs {pb.width}x{pb.height}x{pb.n}")


def diff_ratio(
    a: Image,
    b: Image,
    region: pymupdf.Rect | tuple[float, float, float, float] | None = None,
    *,
    outside: bool = False,
    tol: int = 24,
    margin: float = 0.0,
) -> float:
    """Fraction of pixels whose colour differs by more than `tol` in any channel.

    - region=None: whole page.
    - region=rect: only pixels inside the rect (PDF points, unrotated page space).
    - outside=True: only pixels outside the rect.
    - margin (points) grows the rect first. With outside=True this forgives
      anti-aliasing bleed from glyphs touching the bbox edge; 1-2 pt is plenty.

    `tol` absorbs anti-aliasing/rounding noise: identical vector content
    rendered twice gives 0; sub-pixel glyph shifts mostly stay below ~24 except
    on edges, so use a ratio threshold, not == 0, for "looks the same" checks.
    """
    pa, ma = _parts(a)
    pb, _ = _parts(b)
    _check_same(pa, pb)
    w, h = pa.width, pa.height
    if region is None:
        box = (0, 0, w, h)
        outside = False
    else:
        box = _pixel_box(region, ma, w, h, margin)
    x0, y0, x1, y1 = box
    box_area = max(0, x1 - x0) * max(0, y1 - y0)

    if np is not None:
        m = diff_mask(pa, pb, tol)
        inside_count = int(m[y0:y1, x0:x1].sum())
        if region is None or not outside:
            return inside_count / box_area if box_area else 0.0
        total = w * h - box_area
        return (int(m.sum()) - inside_count) / total if total else 0.0

    # Pure-Python fallback (slow, but correct).
    n = pa.n
    sa, sb = pa.samples_mv, pb.samples_mv
    stride = pa.stride
    count = 0
    total = 0
    for y in range(h):
        row = y * stride
        in_y = y0 <= y < y1
        for x in range(w):
            inside = in_y and x0 <= x < x1
            if inside == outside:
                continue
            total += 1
            o = row + x * n
            for c in range(n):
                if abs(sa[o + c] - sb[o + c]) > tol:
                    count += 1
                    break
    return count / total if total else 0.0


def save_side_by_side(
    before: Image,
    after: Image,
    path: str | os.PathLike[str],
    region: pymupdf.Rect | tuple[float, float, float, float] | None = None,
    *,
    tol: int = 24,
) -> None:
    """Write a PNG: before | after | diff (faded `after`, differing pixels in red,
    `region` outlined in blue on all three panels)."""
    pa, ma = _parts(before)
    pb, _ = _parts(after)
    _check_same(pa, pb)
    w, h = pa.width, pa.height
    gap = 8
    W = 3 * w + 2 * gap
    if np is None:  # pragma: no cover - degrade to two separate PNGs
        p = Path(path)
        pa.save(str(p.with_name(p.stem + "_before.png")))
        pb.save(str(p.with_name(p.stem + "_after.png")))
        return
    A = _arr(pa)
    B = _arr(pb)
    m = (np.abs(A.astype(np.int16) - B.astype(np.int16)) > tol).any(axis=2)
    faded = (255 - (255 - B.astype(np.uint16)) // 4).astype(np.uint8)
    faded[m] = (230, 0, 0)
    canvas = np.full((h, W, 3), 200, dtype=np.uint8)
    canvas[:, 0:w] = A
    canvas[:, w + gap : 2 * w + gap] = B
    canvas[:, 2 * (w + gap) :] = faded
    if region is not None:
        x0, y0, x1, y1 = _pixel_box(region, ma, w, h, 0)
        x1, y1 = max(x0, x1 - 1), max(y0, y1 - 1)
        for off in (0, w + gap, 2 * (w + gap)):
            canvas[y0 : y1 + 1, off + x0] = (0, 90, 255)
            canvas[y0 : y1 + 1, off + x1] = (0, 90, 255)
            canvas[y0, off + x0 : off + x1 + 1] = (0, 90, 255)
            canvas[y1, off + x0 : off + x1 + 1] = (0, 90, 255)
    out = pymupdf.Pixmap(pymupdf.csRGB, W, h, canvas.tobytes(), False)
    out.save(str(path))


def visual_out_dir() -> Path | None:
    d = os.environ.get(ENV_OUT)
    if not d:
        return None
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p


def dump_if_requested(
    name: str,
    before: Image,
    after: Image,
    region: pymupdf.Rect | tuple[float, float, float, float] | None = None,
) -> Path | None:
    """If $XPDF_VISUAL_OUT is set, save a side-by-side PNG named after `name`."""
    out = visual_out_dir()
    if out is None:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")[:150] or "visual"
    path = out / f"{safe}.png"
    save_side_by_side(before, after, path, region)
    return path
