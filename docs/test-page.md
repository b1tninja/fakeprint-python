# The print test page and the received-document analyzer

Two pieces, both pure Python (Pillow + pypdfium2 + fakescan at call time; nothing here ships a page):

* `fakeprint.testpage` - parameters, a size-parametric layout adapter, and a deterministic vector PDF / PNG writer.
* `fakeprint.testpage_analyze` - given the document a printer RECEIVED, report what happened to the page on the way.

![the A4 test page (fakescan layout v3)](test-page-a4.png)

*(rendered by `render_pdf(PageParams(paper="a4")).png(100)`; documentation only, no test reads this file.)*

## Design rules

1. **No fixtures.** The page (PDF or PNG), its layout JSON and its QR payload are produced from `PageParams`: paper name or custom
   `WxH` mm, orientation, printable-area margin, bleed, dpi, colour / mono, page / pages, copy / copies, front / back, job tag. The same
   parameters give the same bytes (no dates, fixed `/ID`, Flate level 6). The sheet names known: A0-A6, ISO B3-B6, JIS B4-B6, Letter,
   Legal, Tabloid, Executive, Statement, any `210x297`.
2. **One layout engine, and it is fakescan's.** What is on the page and where (millimetres, scaled to the sheet, elements that do not fit
   marked `drawn: false` with a `reason`, a `scale` on every element) comes from fakescan's parametric layout **v3** (default pattern `diag`,
   `docs/diag-page-v3.md` in fakescan), together with the ground truth the analyzer checks. fakeprint-python owns only the PDF writer
   (`PdfWriter`, draws primitives by their mm coordinates at ANY sheet size) and the analyzer. `testpage.layout_for(params)` is the single
   adapter:

   * **v3** (`layout["source"] == "fakescan.diag v3"`): `fakescan.diag.diag_page_layout(dpi, **params)` for the layout and ground truth,
     `diag_page_primitives(layout)` for the vector drawing, translated to the writer's schema (`layout["pdf_primitives"]`, paint order;
     the same primitives per element under `elements[i]["primitives"]`). `PageParams` -> fakescan parameters (`v3_kwargs`): paper name (or
     `width_mm` x `height_mm`), orientation, margin, bleed, `page` (1-based here, 0-based there), `total` = `pages`, `copy`, `side`, `job`
     (upper-cased to fakescan's alphabet), `colour="mono"` -> `gray`. Translation: `rect polygon line circle` one to one; `grating` and
     `ticks` expand to the rectangles fakescan paints; **text** is fakescan's 5x7 pixel font, whose cells are read from fakescan's render at the cell
     centres and written as rectangles (exact glyphs, mirrored where fakescan mirrors them, vector, no copy of the font table here); the
     raster-only ops (`ramp hue_wheel siemens_star zone_plate halftone qr`) are cropped from that render and embedded as images at their
     exact physical rectangle. The render is 400 dpi (lower on sheets above A3 so it stays under 36 MP); the layout is generated at the same
     density so crop edges fall on pixel edges. A4 is about 1.8 MB, mostly the zone plate / wheel / star / ramps; text, frames, patches, rulers,
     glyphs, crosses and markers are vector.
   * **fallback, clearly marked** (`layout["source"] == "fakescan.diag v2 fallback"`, only when the installed extension has no v3 --
     `testpage.v3_available()` is false): the fixed layout v2 embedded as one raster at the sheet's top-left corner. Portrait only
     (`LayoutUnavailable` for landscape); on Letter it is clipped by the sheet (`layout["fallback"]["clipped_mm"]`). No glyphs, stamp or
     duplex set. Remove it when every extension in use has v3.

3. **The writer is tested without any layout of ours**: `tests/test_testpage.py` feeds it a six-primitive double at A4, Letter, A5, A3,
   Legal, 123 x 456 mm and A4 / Letter landscape and samples the rendered pixels at the mm positions. The v3 page is tested at nine
   sheets (below).

## What is on the page, and why

Everything below is on the v3 page at every size (an element that does not fit the sheet is dropped with a recorded reason; the mandatory
rows and the QR are never dropped). The full element table is in fakescan's `docs/diag-page-v3.md`.

| Element | Why it is there |
|---|---|
| page border (hairline at the sheet edge), printable-area frame (blue, `margin_mm`), corner ticks, edge ticks | which edge survived; margins actually printed; a missing frame side = clipped |
| 4 registration crosses + 4 corner markers (private 4x4-bit dictionary) | position and scale to ~0.01 % over 140-250 mm, skew; the analyzer's fit |
| asymmetric glyph per corner (F R G J); the **back** side mirrors each glyph and rotates the assignment | rotation AND mirroring are obvious to a human and unambiguous to a program; page 2 of a duplex job must come out the same way up |
| side marker (front inks the left half, back the right, label BACK), page number (big digits), order bars (page bars of 16), `PAGE n OF N / COPY c / JOB` stamp | collate, copy and duplex semantics visible on paper and machine-readable |
| QR carrying page / side / paper / px / dpi / margin / copy / of / job (`lay=3`) | machine decode; `diag_spec_from_qr` regenerates the expected page |
| dashed media outlines (A6, A5, Statement, A4, Letter, Legal, A3: only those smaller than the sheet in both directions, anchored top-left) | what fits on which sheet / where a printer places a smaller medium |
| EBU bars, SMPTE, ColorChecker-24, R/G/B/neutral ramps, CMYK-ish ramps, rich black, hue wheel, near-black / near-white / clip, flat fields 10-90 %, halftone ladder | colour fidelity, channel order, mono conversion, clipping, banding, uniformity, dither |
| Siemens star, slanted edges, line pairs, fine grid, zone plate, line-weight ladder (positive and reverse), circles / hexagon | resolution, sharpness, aspect ratio, thinnest line kept |
| measure bar (the longest multiple of 10 mm that fits), metric and inch rulers | scale by eye and on a long baseline |
| text ladder 6-12 pt, pangram, accent sample, reverse text, text on colour, legend | legibility. Text is fakescan's pixel font (uppercase Latin + a few accents): real fonts / Unicode are not on the page |

## `fakeprint.testpage`

```python
from fakeprint import testpage as tp

params = tp.PageParams(paper="a4", orientation="portrait", pages=2, copies=2, job="smoke")
page = tp.render_pdf(params)          # TestPage: .pdf bytes, .layout dict, .meta, .png(dpi)
open("page.pdf", "wb").write(page.pdf)
open("page.png", "wb").write(page.png(150))   # pdfium raster, PNG carries the dpi

doc = tp.render_document(params, expand_copies=True, collate=True, duplex=True)   # p1c1 p2c1 p1c2 p2c2, front/back alternating
```

* `PageParams` is frozen, JSON-able (`to_dict` / `from_dict` / `replace`) and validates; `paper_size("ISO B5")`, `size_mm()`.
* Copies: by default the document carries ONE copy stamped `COPY 1 OF C` and the printer makes the others (what a `copies=C` job means);
  `expand_copies=True` makes the document carry them itself (`collate` p1 p2 p1 p2, else p1 p1 p2 p2). `duplex=True` stamps sides 1, 3, 5
  front and 2, 4, 6 back. The PDF `/Subject` records the parameters and the `[page, copy, side]` sequence so the analyzer knows what to
  expect for every page of a document that came through a client unchanged.
* No daemon endpoint. The daemon is Rust and has no PDF rasteriser or layout engine; serving a page there would mean a second layout engine
  or a stored fixture, both ruled out. A `GET /v1/testpage` can be added when fakescan's generator exists as a Rust function the daemon can
  call (it is the same crate family); until then the generator is this Python module.

## `fakeprint.testpage_analyze`

```python
from fakeprint import testpage_analyze as ta

rep = ta.analyze_document(received_bytes, "application/pdf", params)   # params optional for a PDF made by render_pdf
ta.brief(rep)                                   # the one-screen summary
rep = ta.analyze_document(data, fmt, params, pages="all")   # every page; rep["pages"] lists page / copy / side per page, in order
rep = ta.analyze_raster(ta.Raster(img, dpi_x, dpi_y, None, "image/pwg-raster"), params)   # any raster you decoded yourself
```

Pipeline: rasterise at an exact dpi (pdfium, 4x oversample box-reduced, the sub-pixel sliver cropped; PNG / JPEG / TIFF at their density)
-> regenerate the expected page from the parameters -> pick which of the 8 dihedral moves explains the received page (stretched and
physical-top-left alignments, whichever matches; `ambiguous` flag when two are close) -> undo it -> run `fakescan.diag.analyze_diag_page`
(fit of the registration crosses, per-element measurements; v3 layouts are dispatched to `fakescan.diag3`, driven only by the layout) on the
canonical raster -> margins, clipping, QR, and the page / copy / side stamps READ from the page.

What it reports (`rep["summary"]` unless noted):

| Field | Meaning |
|---|---|
| `is_test_page`, `reason` | false with the reason for foreign documents ("not a fakeprint test page ...") or unreadable ones |
| `document` | pages, per-page MediaBox / CropBox / TrimBox / BleedBox / ArtBox in mm, which box defines the sheet (`effective_box`), `/Rotate`, embedded images (pixels, effective dpi, filters) |
| `verdict_orientation`, `rotation_deg_90s`, `mirrored` | "upright" / "rotated 180 degrees" / "mirrored left-right" / ... (what the path did to the page) |
| `scale_x`, `scale_y`, `skew_rotation_deg` | physical scale per axis (1.0 = right) from the crosses' 190 mm baseline; fine rotation |
| `sheet_mm`, `sheet_size_ok`, `expected_sheet_mm` | received sheet vs the parameters |
| `margins_mm` | where the layout box landed on the sheet (left, top, right, bottom; negative = outside) |
| `clipped_edges`, `layout_beyond_sheet_mm` | frame sides that did not survive, and layout that lies outside the sheet |
| `page`, `page_bars`, `side`, `page_order_ok` | READ from the page by `fakescan.diag3`: the big page-number digits (1-based), the filled order bars, the side marker (`front` / `back`); `page_order_ok` = all three agree with the page the parameters describe (a mismatch is reported, not hidden) |
| `pages`, `copy`, `job`, `qr_text`, `qr_ok` | the QR decoded by `fakeprint.qrdecode` (versions 1-10, RS syndromes checked): `of=`, `copy=`, `job=` |
| `glyph_orientation` | the corner-glyph verdict of the layout-driven analyzer on the canonicalised raster (`upright` / `rot180` / `flip_h` / `flip_v`) |
| `colorchecker_mean_delta_e76`, `channel_order`, `flat_field_max_stdev`, `ruler_*_pitch_error_pct`, `margin_errors_mm` | colour, uniformity, ruler scale and frame-position results of the v3 analyzer |
| `hue_err_deg_max_abs`, `shadows_clipped_steps`, `highlights_clipped_steps`, `res_lines_*_last_resolvable_period_mm`, ... | colour / ramp / resolution results; the whole per-element report is `rep["elements"]` with `detail="full"` |
| `fit` = `"failed"` | fewer than 3 of the 4 registration crosses found (corners cut off; crosses scoring under 40 % of the best are clutter, not crosses) or a poor fit: orientation only, no measurements |

Limits, stated: the resolution figures are limited by the analysis dpi (150 by default; raise `dpi`), a printer's halftoning and a scan's
noise are not modelled (it reads documents, not paper), the QR decoder does not correct errors, and it is pure Python (about 1.5 s a page,
`detail="fast"` skips the per-element report).

### From a job

```python
c = srv.client()                       # or fakeprint.Client(base_url)
c.job_analysis(job_id)["summary"]      # lazy, Python-side: reads the stored document from the job row's path
c.job(job_id, analysis=True).analysis  # the same summary on the Job row
```

The daemon (Rust) shows job rows only; it computes nothing about the received page and has no thumbnail (there is no PDF rasteriser in
the daemon; a thumbnail would need one). `job_analysis` needs read access to the stored file (embedded `Server` or a daemon on the same
machine).

## Tests

`tests/test_testpage.py` (no fixtures): paper registry, the writer at eight sheet shapes, determinism, mono, PNG dpi, the adapter (v3 source,
parameter mapping, the marked v2 fallback), the QR decoder (against fakescan's own QR, which also validates the tables: syndromes are zero),
and, on fakescan layout v3 at A4, Letter, A5, A3, Legal, 123 x 456 mm, A4 / Letter landscape and A3 landscape mono: every sample of the layout
(>100 per page) against the PDF rasterised through pdfium, then the analyzer (upright, scale 1.000 +- 0.002, no clipping, sheet size, frame margin,
QR, page / bars / side / copy / pages read back); orientation faults (rotations, mirrors, transpose) injected into the raster at four of those
sheets, and the corner glyphs alone naming rot180 / flip_h / flip_v; the BACK side (mirrored glyphs, right-half marker, `side=back` read); a
stamp that disagrees with the expected page; a duplex / collated document read page by page; the analyzer on scaled, cropped, cut-corner and
foreign documents; `Client.job_analysis` through the embedded server. The cross-client run is in `pdfcraft-fakeprint` (sections k, and the
v3 phase of the print loop).

## Known limits of the v3 analyzer as used here

* `fakescan.diag3` finds the printable frame as "blue minus red": on a mono (gray) page every frame edge reads as clipped, so the analyzer does
  not report frame clipping for `colour="mono"` (the margins and layout-beyond-sheet figures still apply).
* `fakescan.diag3._fit` accepts any local maximum as a cross; with three of four crosses cut off it fits clutter with a residual of ~0. The wrapper
  rejects crosses that score under 40 % of the best one.
* Text on the page is the 5x7 pixel font; there is no real Unicode or TrueType sample.
