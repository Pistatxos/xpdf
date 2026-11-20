# xpdf test corpus

Real and synthetic PDFs used to test xpdf's text editing (detect paragraphs →
redact → re-insert with `insert_htmlbox`) and its visual fidelity.
Total size is about 2.3 MB (the budget, enforced by a test, is 8 MB).

- `manifest.json` is the source of truth: category, tags, origin, source URL,
  license, pages kept, and the minimum word count per page. `tests/conftest.py`
  loads it. Tests that take a `sample` argument run once per file, and you can
  narrow them with `@pytest.mark.samples("word", "justified")` or
  `@pytest.mark.samples(origin="real")`. The `samples` fixture returns the list.
- `make_samples.py` rebuilds the synthetic files. `--fetch` downloads the real
  ones again into a temp dir and trims them. `--facts` prints the font table below.
- Each real file is the original PDF with pages removed (`doc.select`, or
  `insert_pdf(widgets=True)` for forms). The struct tree, outlines and name
  trees are removed so that the dropped pages don't stay embedded. XFA is also
  removed from the W-9. Fonts, content streams and resources are byte-for-byte
  the originals.

## Files

| File | Category | Origin | Pages (of orig.) | Source / license | Why it is here |
|---|---|---|---|---|---|
| `word_court_opinion_justified.pdf` | word | real | 0, 4 of 12 | [5th Cir. 23-50697](https://www.ca5.uscourts.gov/Opinions/pub/23/23-50697-CV0.pdf), public domain (US court) | Word document exported with Adobe PDF Library (PDFMaker). Body text is **fully justified** using `Tw`/`Tc`. It has italic runs inside paragraphs (case names), Equity Text/Caps TrueType subsets in WinAnsi, and footnotes. An iText **header stamp** in non-embedded Helvetica sits on top. |
| `word_omb_memo.pdf` | word | real | 0, 1, 3 of 34 | [OMB M-24-10](https://www.whitehouse.gov/wp-content/uploads/2024/03/M-24-10-Advancing-Governance-Innovation-and-Risk-Management-for-Agency-Use-of-Artificial-Intelligence.pdf), public domain | Word LTSC. Ragged-right paragraphs, roman-numeral lists, footnotes, and a seal image. Times New Roman/Arial/Calibri appear in **three flavours**: CID Identity-H subsets, WinAnsi subsets, and **non-embedded** TrueType. |
| `word365_aptos_math.pdf` | word | real | 0 | [pdf.js test corpus](https://github.com/mozilla/pdf.js/blob/master/test/pdfs/bug1937438_from_word.pdf), Apache-2.0 repo | Modern Word 365 with Aptos and Cambria Math. The same font is embedded **twice** (WinAnsi + Identity-H). |
| `latex_revtex_twocol.pdf` | latex | real | 0, 1 of 7 | [arXiv:2611.21676](https://arxiv.org/abs/2611.21676), CC BY 4.0 | pdfTeX/REVTeX in **two columns**, with Computer Modern Type1 (`pfa`) subsets, math, fi/fl ligatures, 26 hyphenated line ends on p0, and the arXiv side watermark (a Form XObject holding vertical text). |
| `latex_lualatex_stix.pdf` | latex | real | 0 | [pdf.js test corpus](https://github.com/mozilla/pdf.js/blob/master/test/pdfs/bug1937438_af_from_latex.pdf), Apache-2.0 repo | LuaLaTeX with OpenType STIX Two as **CFF CID Identity-H**, plus unicode-math. |
| `scan_ocr_omb_memo.pdf` | scan-ocr | real | 0 of 13 | [OMB M-25-22](https://www.whitehouse.gov/wp-content/uploads/2025/02/M-25-22-Driving-Efficient-Acquisition-of-Artificial-Intelligence-in-Government.pdf), public domain | A real scan: RICOH copier, 150 dpi JPEG, then Acrobat Paper Capture OCR. **All text is render mode 3**, with `Tc` per word to stretch it over the image. It contains real OCR errors (`R~ssell`, `Duector`) and a `HiddenHorzOCR` CID font. |
| `form_irs_w9.pdf` | form | real | 0 of 6 | [IRS W-9](https://www.irs.gov/pub/irs-pdf/fw9.pdf), public domain | AcroForm with 23 widgets (text fields and checkboxes). Originally made in LiveCycle Designer; XFA removed. Helvetica Neue / Franklin Gothic **CFF (Type1C)** subsets. Has vertical side text. |
| `nonlatin_greek_wikipedia.pdf` | non-latin | real | 20 of 25 | [el.wikipedia «Αθήνα»](https://el.wikipedia.org/wiki/Αθήνα), CC BY-SA 4.0 | **Greek**. Chrome/Skia PDF export, Liberation TrueType as Identity-H subsets, links. |
| `nonlatin_russian_wikipedia.pdf` | non-latin | real | 13 of 39 | [ru.wikipedia «Байкал»](https://ru.wikipedia.org/wiki/Байкал), CC BY-SA 4.0 | **Cyrillic**. Same producer as the Greek file. |
| `cjk_type3_japanese_wikipedia.pdf` | non-latin | real | 14 of 41 | [ja.wikipedia «富士山»](https://ja.wikipedia.org/wiki/富士山), CC BY-SA 4.0 | **Japanese drawn with 118 unnamed Type3 fonts** (Skia falls back to Type3 for the CJK font). This is the only real Type3 file and the largest one (0.9 MB of glyph procedures). |
| `arabic_cid_truetype.pdf` | non-latin | real | 0 | [pdf.js test corpus](https://github.com/mozilla/pdf.js/blob/master/test/pdfs/ArabicCIDTrueType.pdf), Apache-2.0 repo | **Arabic (RTL)** from Apple pstopdffilter, in 4 fonts × (Identity-H + WinAnsi). |
| `cjk_vertical_identity_v.pdf` | non-latin | real | 0–2 | [pdf.js test corpus](https://github.com/mozilla/pdf.js/blob/master/test/pdfs/vertical.pdf), Apache-2.0 repo | **Vertical Japanese**: `Identity-V`, line `dir` = (0, 1), made with dvipdfmx. Pages 1–2 have no extractable text. |
| `rotated_text_calc.pdf` | rotated | real | 0 | [pdf.js test corpus](https://github.com/mozilla/pdf.js/blob/master/test/pdfs/rotated.pdf), Apache-2.0 repo | OpenOffice Calc. Text at 0°, 90° and 45°, set in a symbolic ArialMT subset with no `/Encoding`. |
| `synth_styled_paragraphs.pdf` | synthetic-word-like | synthetic | – | `make_samples.py`, CC0 | Justified, centred and right-aligned paragraphs, bold/italic/bold-italic runs, a bullet list, header and footer. Built with `insert_htmlbox` (Charis SIL), so **the body text sits inside a Form XObject**. Known ground truth. |
| `synth_ocr_scan.pdf` | scan-ocr | synthetic | – | `make_samples.py`, CC0 | Grey JPEG of the text with an exactly aligned invisible (mode 3) Times layer. The ground truth is `OCR_LINES`. |
| `synth_text_over_background.pdf` | background | synthetic | – | `make_samples.py`, CC0 | A paragraph over a gradient image and a semi-transparent rectangle, plus a title directly on the image. Body text uses an embedded Identity-H Helvetica subset. |
| `synth_rotated_pages.pdf` | rotated | synthetic | – | `make_samples.py`, CC0 | Three pages with `/Rotate` 90/180/270, each with a justified paragraph (`Tw`) and a box. |

## Fonts and paragraph facts

The `has_glyph` column counts the distinct non-space characters shown with
that font that `pymupdf.Font(fontbuffer=doc.extract_font(xref)[3]).has_glyph(ord(c))`
finds. "n/a" means the font is not embedded, or is Type3 (it has no font
program). Run `uv run python tests/samples/make_samples.py --facts` to get the
full table.

| File | Font (subset prefix stripped) | Type / encoding | Emb. | Subset | has_glyph |
|---|---|---|---|---|---|
| word_court_opinion_justified | EquityTextA, -Italic, EquityCapsA, ArialMT, OldEnglishTextMT | TrueType WinAnsi | yes | yes | 61/61, 28/28, 34/34, 31/31, 20/20 ✅ |
| | Helvetica (iText stamp) | Type1 WinAnsi | **no** | – | n/a |
| word_omb_memo | TimesNewRoman, ,Bold, ,Italic, Arial | **Type0 Identity-H** (ttf) | yes | yes | **0/67, 0/35, 0/29, 0/26** ❌ |
| | TimesNewRomanPSMT | Type0 Identity-H | yes | no | 57/57 ✅ |
| | TimesNewRomanPSMT, -BoldMT, -BoldItalicMT, Arial-BoldMT | TrueType WinAnsi | **no** | – | n/a |
| | Calibri | TrueType WinAnsi | yes | yes | 1/1 |
| word365_aptos_math | Aptos (×2: WinAnsi + Identity-H) | TrueType / Type0 | yes | yes | 18/18 ✅ |
| | CambriaMath | Type0 Identity-H | yes | yes | 5/8 (math alphanumerics) |
| latex_revtex_twocol | CMR10, CMR9, CMTI9, CMBX12, CMMI10… | Type1 (`pfa`), builtin encoding | yes | yes | 70/70, 43/43… ✅ (CMSY10 10/12, CMEX10 6/7) |
| | Courier*, Helvetica*, Times* (arXiv stamp) | Type1 WinAnsi | no | – | n/a |
| | NimbusRoman-Regular (stamp) | Type0 Identity-H (CFF) | yes | yes | 9/26 |
| latex_lualatex_stix | STIXTwoText-Regular/Bold, STIXTwoMath | Type0 Identity-H (**CFF CID**) | yes | yes | **0/32, 0/8, 0/7** ❌ |
| scan_ocr_omb_memo | Times-Roman/-Bold/-Italic, Helvetica | Type1 WinAnsi | no | – | n/a (render mode 3) |
| | HiddenHorzOCR | Type0 Identity-H | yes | no | 0/1 |
| form_irs_w9 | HelveticaNeueLTStd-Roman/-Bd/-It/-BdIt/-BlkCn | Type1 (**CFF**) WinAnsi | yes | yes | 68/68, 53/53, 38/38, 22/22, 3/3 ✅ |
| nonlatin_greek_wikipedia | LiberationSerif, LiberationSans(-Bold) | Type0 Identity-H (ttf) | yes | yes | 67/67, 15/15, 18/18 ✅ |
| nonlatin_russian_wikipedia | LiberationSerif(-Bold), LiberationSans-Bold | Type0 Identity-H (ttf) | yes | yes | 72/72, 7/7, 27/27 ✅ |
| cjk_type3_japanese_wikipedia | 118 × unnamed | **Type3** | – | – | n/a |
| | LiberationSerif | Type0 Identity-H | yes | yes | 16/16 |
| arabic_cid_truetype | Jeezah, NewBasrahBold, Beirut, DamascusBold | Type0 Identity-H + TrueType WinAnsi | yes | yes | **0/13** each ❌ |
| cjk_vertical_identity_v | AokinMincho | Type0 **Identity-V** | yes | yes | **0/8** ❌ |
| rotated_text_calc | ArialMT | TrueType, **no /Encoding** (symbolic) | yes | yes | **0/11** ❌ |
| synth_styled_paragraphs | Charis SIL (4 styles), Nimbus Sans Bold | Type0 Identity-H | yes | yes | all ✅ |
| synth_text_over_background | Helvetica (TextWriter) / Helvetica-Bold | Type0 Identity-H / base14 | yes / no | yes / – | 28/28 / n/a |
| synth_ocr_scan, synth_rotated_pages | Times-Roman / Helvetica | base14 WinAnsi | no | – | n/a |

Writing directions and render modes:
- Every line has `dir` (1, 0) except in these files: `cjk_vertical_identity_v` (0, 1); `rotated_text_calc` (0, 1) and (0.71, −0.71); the arXiv watermark in `latex_revtex_twocol` (0, −1); and two vertical side labels in `form_irs_w9` (0, −1).
- Render mode is 0 everywhere except `scan_ocr_omb_memo` and `synth_ocr_scan`, where all text is mode 3.
- `synth_rotated_pages` has page `/Rotate` values 90/180/270. Text `dir` is still (1, 0), because it is in unrotated space.

## Traps found in the real files

1. **Embedded subsets often have no usable cmap.** Word's Identity-H TrueType subsets, LuaTeX CFF CID fonts, the Arabic and Identity-V fonts, and the symbolic Calc ArialMT all return `has_glyph() == 0` for characters they visibly draw. To reuse them you have to go through ToUnicode → CID → GID, or substitute a font. A WinAnsi or Type1 subset generally works, but it only contains the glyphs that were used.
2. **Non-embedded fonts are common**: Word's WinAnsi TrueType (TimesNewRomanPSMT…), stamps, and the OCR layer. Substitution is unavoidable.
3. **Ligatures**: in CM fonts `ﬁ`/`ﬂ` are never extracted as U+FB01/FB02, not even with `TEXT_PRESERVE_LIGATURES`. ToUnicode expands them to two chars, which share or split the glyph bbox. Sometimes the second char has the same origin as the first, so you get **zero-width chars**. Arabic text comes out as **presentation forms** (U+FExx, U+FCCB).
4. **Hyphenation**: the two-column LaTeX file has ~25 lines per page ending in `-`. Reflowing must decide whether that hyphen is soft or real.
5. **Spacing tricks**: the justified Word text uses `Tw` plus `Tc` per line. The OCR layer uses `Tc` per word to fit the boxes. LaTeX uses `TJ` kerning everywhere. Word also **bolds spaces**: spans in `EquityTextA-Bold` that contain only " ".
6. **Text lives in Form XObjects**: the arXiv watermark, and all `insert_htmlbox` output (`synth_styled_paragraphs`).
7. **Fonts are duplicated across encodings**: the same face is embedded twice (Aptos and the Arabic fonts as WinAnsi + Identity-H). A single paragraph can therefore reference two different font xrefs that have the same name.
8. **Overlaid stamps** (iText header in Helvetica on the court opinion; the arXiv side stamp) look like paragraphs but aren't part of the body.
9. **Type3**: Japanese text in 118 anonymous Type3 fonts. There is no font program to reuse, so it must be substituted (e.g. Noto CJK).
10. **Redaction defaults damage backgrounds**: `apply_redactions()` blanks images and line art under the rect. With `images=PDF_REDACT_IMAGE_NONE, graphics=PDF_REDACT_LINE_ART_NONE`, every sample stays pixel-identical outside the redacted block. `test_samples.py` checks both.
11. `Document.select()` (PyMuPDF 2.1) **drops the catalog's /AcroForm**. Saving after page deletion silently unforms a PDF. `make_samples.py` works around it with `insert_pdf(widgets=True)`.
