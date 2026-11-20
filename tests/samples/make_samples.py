"""Builds the xpdf test corpus in tests/samples/.

Usage (from the repo root):

    uv run python tests/samples/make_samples.py            # (re)generate synthetic samples
    uv run python tests/samples/make_samples.py --fetch    # also re-download + trim real samples
    uv run python tests/samples/make_samples.py --facts    # print the font/paragraph facts table

Real samples are listed in manifest.json (source URL, license, pages kept).
``--fetch`` downloads each one into a fresh temporary directory, opens it with
PyMuPDF only, keeps the listed pages and strips document-level structures
that would otherwise drag every page's objects along (struct tree, outlines,
name trees, XFA). Fonts, content streams and page resources are untouched.

The synthetic samples are deterministic and carry their ground truth in this
file (constants below), so tests can compare against known text.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pymupdf

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"

# --------------------------------------------------------------------------
# Ground truth for synthetic samples
# --------------------------------------------------------------------------

LOREM_P1 = (
    "The quick brown fox jumps over the lazy dog while the committee reviews "
    "the annual report. Every paragraph in this document is justified, and some "
    "words are <b>bold</b>, some are <i>italic</i> and a few are "
    "<b><i>bold italic</i></b>, exactly like a typical word processor export. "
    "Editing this paragraph must keep the runs, the alignment and the line height."
)
LOREM_P2 = (
    "A second paragraph follows with a different length so that the last line "
    "is short. Office documents often contain long sentences with numbers such "
    "as 1,234.56 EUR, dates like 20/11/2026 and punctuation (parentheses, "
    "quotes “like these”, dashes – and —) that a reflow "
    "engine has to keep on the right line."
)
LIST_ITEMS = ["First item of the list", "Second item, slightly longer than the first one", "Third item"]
HEADER_TEXT = "ACME Corp. — Internal report"
FOOTER_TEXT = "Page 1 of 1"

OCR_LINES = [
    "MEMORANDUM FOR ALL STAFF",
    "Subject: Quarterly review of document workflows",
    "",
    "This page simulates a scanned letter. The visible text is a raster",
    "image; an invisible OCR text layer (render mode 3) sits on top so",
    "that the page is searchable and selectable. Editing must not paint",
    "new visible text over the scan unless the user asks for it.",
    "",
    "The review will take place on Monday at 10:00 in room 4B. Please",
    "bring the printed copies of the last three reports.",
]

BACKGROUND_TEXT = (
    "This paragraph is drawn on top of a gradient background image and a "
    "coloured rectangle. Redacting it to rewrite the text must not punch a "
    "white hole in the background: the image and the shapes behind the "
    "words have to stay exactly as they were, pixel for pixel."
)
BACKGROUND_TITLE = "Text over a background"

ROTATED_TEXT = (
    "This page has a /Rotate value of {rot} degrees. The paragraph is written in "
    "unrotated page space, so the viewer shows it turned. Coordinates from "
    "get_text are in unrotated space; the derotation matrix maps them to the screen."
)


def _base14_html(body: str) -> str:
    return f"<div style='font-family: serif; font-size: 11pt; line-height: 1.3'>{body}</div>"


def make_styled_paragraphs(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 40), HEADER_TEXT, fontname="helv", fontsize=9, color=(0.3, 0.3, 0.3))
    page.draw_line((72, 46), (523, 46), color=(0.6, 0.6, 0.6), width=0.5)
    page.insert_text((270, 815), FOOTER_TEXT, fontname="helv", fontsize=9, color=(0.3, 0.3, 0.3))
    html = (
        "<h2 style='font-family: sans-serif'>Annual report</h2>"
        f"<p style='text-align: justify'>{LOREM_P1}</p>"
        f"<p style='text-align: justify'>{LOREM_P2}</p>"
        "<ul>" + "".join(f"<li>{t}</li>" for t in LIST_ITEMS) + "</ul>"
        "<p style='text-align: center'><i>Centered italic closing line.</i></p>"
        "<p style='text-align: right'>Right-aligned signature</p>"
    )
    page.insert_htmlbox(pymupdf.Rect(72, 70, 523, 700), _base14_html(html))
    doc.set_metadata({"title": "xpdf synthetic styled paragraphs", "producer": "xpdf make_samples.py"})
    doc.subset_fonts()
    doc.save(path, garbage=4, deflate=True)


def make_ocr_scan(path: Path) -> None:
    """Visible text as a grayscale raster + invisible text layer on top."""
    w, h = 595, 842
    src = pymupdf.open()
    sp = src.new_page(width=w, height=h)
    y = 90.0
    positions: list[tuple[float, str]] = []
    for line in OCR_LINES:
        if line:
            sp.insert_text((72, y), line, fontname="tiro", fontsize=12)
            positions.append((y, line))
        y += 18
    # Slight skew/noise is not needed; a mild blur through low dpi is enough.
    pix = sp.get_pixmap(dpi=120, colorspace=pymupdf.csGRAY)
    img = pix.tobytes("jpeg", jpg_quality=70)

    doc = pymupdf.open()
    page = doc.new_page(width=w, height=h)
    page.insert_image(page.rect, stream=img)
    for y, line in positions:
        page.insert_text((72, y), line, fontname="tiro", fontsize=12, render_mode=3)
    doc.set_metadata({"title": "xpdf synthetic OCR scan", "producer": "xpdf make_samples.py"})
    doc.save(path, garbage=4, deflate=True)


def make_text_over_background(path: Path) -> None:
    w, h = 595, 420
    # Gradient background image built from raw samples (no PIL needed).
    iw, ih = 256, 128
    samples = bytearray()
    for yy in range(ih):
        for xx in range(iw):
            samples += bytes((40 + xx * 180 // iw, 90 + yy * 120 // ih, 200 - xx * 120 // iw))
    pix = pymupdf.Pixmap(pymupdf.csRGB, iw, ih, bytes(samples), False)

    doc = pymupdf.open()
    page = doc.new_page(width=w, height=h)
    page.insert_image(page.rect, pixmap=pix, keep_proportion=False)
    page.draw_rect(pymupdf.Rect(50, 120, 545, 300), color=None, fill=(1.0, 0.95, 0.7), fill_opacity=0.85)
    page.draw_circle((500, 80), 40, color=(0.8, 0.1, 0.1), fill=(0.95, 0.4, 0.3))
    page.insert_text((60, 90), BACKGROUND_TITLE, fontname="hebo", fontsize=22, color=(1, 1, 1))
    # TextWriter embeds the font program (Identity-H), so the body text uses
    # an embedded, subsetted font, unlike the base-14 title.
    font = pymupdf.Font("helv")
    tw = pymupdf.TextWriter(page.rect)
    tw.fill_textbox(pymupdf.Rect(66, 136, 529, 290), BACKGROUND_TEXT, font=font, fontsize=13, align=pymupdf.TEXT_ALIGN_LEFT)
    tw.write_text(page, color=(0.1, 0.1, 0.3))
    doc.set_metadata({"title": "xpdf synthetic text over background", "producer": "xpdf make_samples.py"})
    doc.subset_fonts()
    doc.save(path, garbage=4, deflate=True)


def make_rotated_pages(path: Path) -> None:
    doc = pymupdf.open()
    for rot in (90, 180, 270):
        page = doc.new_page(width=595, height=842)
        page.insert_textbox(
            pymupdf.Rect(72, 72, 523, 300),
            ROTATED_TEXT.format(rot=rot),
            fontname="helv",
            fontsize=12,
            align=pymupdf.TEXT_ALIGN_JUSTIFY,
        )
        page.draw_rect(pymupdf.Rect(72, 320, 200, 380), color=(0, 0, 1), fill=(0.8, 0.9, 1))
        page.set_rotation(rot)
    doc.set_metadata({"title": "xpdf synthetic rotated pages", "producer": "xpdf make_samples.py"})
    doc.save(path, garbage=4, deflate=True)


SYNTHETIC = {
    "synth_styled_paragraphs.pdf": make_styled_paragraphs,
    "synth_ocr_scan.pdf": make_ocr_scan,
    "synth_text_over_background.pdf": make_text_over_background,
    "synth_rotated_pages.pdf": make_rotated_pages,
}

# --------------------------------------------------------------------------
# Real samples: download + trim
# --------------------------------------------------------------------------


def download_url(entry: dict[str, Any]) -> str:
    src: str = entry["source_url"]
    m = re.search(r"exported via (\S+)\)", src)
    if m:
        url = m.group(1)
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit(parts._replace(path=urllib.parse.quote(parts.path)))
    if src.startswith("https://github.com/") and "/blob/" in src:
        return src.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/")
    if src.startswith("https://arxiv.org/abs/"):
        return src.replace("/abs/", "/pdf/")
    return src


def trim(src: Path, dst: Path, pages: list[int]) -> None:
    doc = pymupdf.open(src)
    if doc.is_form_pdf:
        # Document.select() drops the catalog's /AcroForm (PyMuPDF 2.1), so
        # forms are copied page by page with their widgets instead.
        new = pymupdf.open()
        for pno in pages:
            new.insert_pdf(doc, from_page=pno, to_page=pno, widgets=True)
        new.set_metadata({k: v for k, v in doc.metadata.items() if k not in ('format', 'encryption')})
        doc = new
    else:
        doc.select(pages)
    cat = doc.pdf_catalog()
    for key in ("StructTreeRoot", "MarkInfo", "Outlines", "Names", "Dests", "PageLabels"):
        doc.xref_set_key(cat, key, "null")
    kind, val = doc.xref_get_key(cat, "AcroForm")
    if kind == "xref" and doc.xref_get_key(int(val.split()[0]), "XFA")[0] != "null":
        # XFA describes the full original form; after dropping pages it would
        # be inconsistent. Keep the plain AcroForm only. (Setting the path
        # "AcroForm/XFA" on the catalog would replace the indirect AcroForm
        # dict by a new direct one and lose /Fields.)
        doc.xref_set_key(int(val.split()[0]), "XFA", "null")
    for page in doc:
        doc.xref_set_key(page.xref, "StructParents", "null")
    doc.save(dst, garbage=4, deflate=True)


def fetch_real(entries: list[dict[str, Any]]) -> None:
    with tempfile.TemporaryDirectory(prefix="xpdf-samples-") as tmp:
        for e in entries:
            if e["origin"] != "real":
                continue
            url = download_url(e)
            raw = Path(tmp) / ("raw_" + e["file"])
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (xpdf test corpus)"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw.write_bytes(r.read())
            trim(raw, HERE / e["file"], e["pages"])
            print(f"fetched {e['file']:40s} {(HERE / e['file']).stat().st_size // 1024:6d} KB  <- {url}")


# --------------------------------------------------------------------------
# Facts table (for README.md)
# --------------------------------------------------------------------------


def _strip_subset(name: str) -> str:
    return re.sub(r"^[A-Z]{6}\+", "", name)


def used_chars(doc: pymupdf.Document) -> dict[str, set[str]]:
    """Distinct characters per font name (subset prefix stripped), as extracted."""
    used: dict[str, set[str]] = collections.defaultdict(set)
    for page in doc:
        for b in page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_LIGATURES)["blocks"]:
            for line in b.get("lines", []):
                for span in line["spans"]:
                    used[_strip_subset(span["font"])].update(
                        ch["c"] for ch in span["chars"] if not ch["c"].isspace()
                    )
    return used


def font_facts(doc: pymupdf.Document) -> list[dict[str, Any]]:
    """Per font: type, encoding, embedding, and how many of the characters
    actually shown with it can be found again via
    ``pymupdf.Font(fontbuffer=extract_font(...)).has_glyph(ord(c))``.
    That ratio tells whether the embedded font can be reused to re-insert
    the same text (a subset can never render characters it does not contain)."""
    used = used_chars(doc)
    out: dict[int, dict[str, Any]] = {}
    for pno in range(doc.page_count):
        for xref, ext, ftype, basefont, _name, enc, *_ in doc.get_page_fonts(pno, full=True):
            if xref in out:
                continue
            chars = used.get(_strip_subset(basefont), set())
            info: dict[str, Any] = {
                "xref": xref,
                "name": basefont,
                "type": ftype,
                "encoding": enc,
                "embedded": ext not in ("n/a", ""),
                "subset": bool(re.match(r"^[A-Z]{6}\+", basefont)),
                "ext": ext,
                "used": len(chars),
                "has_glyph": None,
            }
            if info["embedded"]:
                try:
                    _n, _e, _t, buf = doc.extract_font(xref)
                    f = pymupdf.Font(fontbuffer=buf)
                    info["has_glyph"] = sum(1 for c in chars if f.has_glyph(ord(c)))
                except Exception as exc:  # noqa: BLE001 - we want the reason
                    info["has_glyph"] = f"error: {type(exc).__name__}"
            out[xref] = info
    return list(out.values())


def page_facts(page: pymupdf.Page) -> dict[str, Any]:
    modes: collections.Counter[int] = collections.Counter()
    for t in page.get_texttrace():
        modes[t["type"]] += len(t["chars"])
    dirs: collections.Counter[tuple[float, float]] = collections.Counter()
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            dirs[(round(line["dir"][0], 2), round(line["dir"][1], 2))] += 1
    return {"render_modes": dict(modes), "dirs": dict(dirs), "rotation": page.rotation}


def print_facts(entries: list[dict[str, Any]]) -> None:
    for e in entries:
        doc = pymupdf.open(HERE / e["file"])
        print(f"\n## {e['file']}  pages={doc.page_count}  producer={doc.metadata.get('producer')!r}")
        for i, page in enumerate(doc):
            print(f"  p{i}: {page_facts(page)}")
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
        for f in font_facts(doc):
            key = (_strip_subset(f["name"]) or "(unnamed)", f["type"], f["encoding"], f["embedded"], f["subset"], f["ext"])
            groups[key].append(f)
        for (name, ftype, enc, emb, sub, ext), fs in groups.items():
            used_n = max(f["used"] for f in fs)
            hg = [f["has_glyph"] for f in fs]
            hg_s = "n/a" if hg[0] is None else (str(hg[0]) if isinstance(hg[0], str) else f"{max(hg)}/{used_n}")
            print(
                f"  font {name[:34]!r:36s} x{len(fs):<3d} {ftype:9s} {enc[:14]!r:16s} "
                f"emb={emb!s:5s} subset={sub!s:5s} ext={ext:4s} has_glyph={hg_s}"
            )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="re-download and trim the real samples")
    ap.add_argument("--facts", action="store_true", help="print font/render-mode facts and exit")
    args = ap.parse_args()
    entries = json.loads(MANIFEST.read_text("utf-8"))["samples"]
    if args.facts:
        print_facts(entries)
        return
    if args.fetch:
        fetch_real(entries)
    for name, fn in SYNTHETIC.items():
        fn(HERE / name)
        print(f"generated {name:40s} {(HERE / name).stat().st_size // 1024:6d} KB")


if __name__ == "__main__":
    main()
