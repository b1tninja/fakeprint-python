"""Received-document analyzer for the fakeprint print test page.

Given the document a printer (or fake) RECEIVED - PDF, PNG, JPEG, TIFF, or any raster you decoded yourself - and the page
parameters, report what happened to the page on the way: sheet size and which PDF page box defines it, scale per axis, rotation
and flip (an eight-way dihedral test against the regenerated page), margins and clipped edges, the page / copy / side stamp,
the QR payload, colour / ramp / hue errors, the resolution limit, and the facts of embedded images.

It is built ON the fakescan ``diag`` machinery, not beside it: the expected page is regenerated from the same parameters
(:mod:`fakeprint.testpage`, whose layout comes from fakescan) and the per-element measurements are
``fakescan.diag.analyze_diag_page`` run on the canonicalised raster.  Foreign documents degrade gracefully: they get
``is_test_page: false`` with the reason and the document facts (page size, boxes, images), nothing is guessed.

Needs Pillow and pypdfium2 (PDF) at call time; everything else is stdlib.  Pure Python, so a page takes seconds, not milliseconds:
``detail="fast"`` skips the per-element report (orientation, fit, margins, clipping, QR only).
"""

from __future__ import annotations

import base64
import io
import json
import math
from dataclasses import dataclass, field
from typing import Any

from .testpage import MM, PageParams, TestPage, layout_for, render_pdf

ANALYSIS_DPI = 150


# ------------------------------------------------------------------------------------------------ rasterising


@dataclass
class Raster:
    img: Any  # Pillow RGB
    dpi_x: float  # EXACT dpi of the pixels
    dpi_y: float
    page_mm: tuple[float, float] | None
    fmt: str
    notes: list[str] = field(default_factory=list)
    declared_dpi: tuple[float, float] | None = None


def render_pdf_exact(data: bytes, dpi: float = ANALYSIS_DPI, oversample: int = 4, page_index: int = 0) -> Raster:
    """Page ``page_index`` of a PDF as pixels at EXACTLY ``dpi`` in both axes (the sub-pixel sliver of the page is cropped instead of
    letting pdfium stretch the page to an integral size).  ``oversample`` > 1 renders larger and box-reduces (default 4: cancels pdfium's whole-pixel
    snapping of image edges, which is 0.09 % of scale at 1 and 0.03 % at 4 on a 1236 px image).  pdfium renders the effective page box (CropBox if present, else MediaBox) and applies /Rotate."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        page = pdf[page_index]
        w, h = page.get_size()
        s = dpi * oversample / 72.0
        W = int(math.floor(w * s + 1e-6)) // oversample * oversample
        H = int(math.floor(h * s + 1e-6)) // oversample * oversample
        cr = 0.0 if abs(w * s - W) < 1e-6 else max(w - W / s, 0.0) + 1e-7
        cb = 0.0 if abs(h * s - H) < 1e-6 else max(h - H / s, 0.0) + 1e-7
        img = page.render(scale=s * (1 - 1e-9), crop=(0, cb, cr, 0)).to_pil().convert("RGB")
        if img.size != (W, H):
            raise AssertionError(f"pdfium rendered {img.size}, wanted {(W, H)}")
        img = img.reduce(oversample)
        return Raster(img, dpi, dpi, (w / 72.0 * MM, h / 72.0 * MM), "application/pdf", [])
    finally:
        pdf.close()


def image_raster(fmt: str, data: bytes) -> Raster:
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    im.load()
    dpi = im.info.get("dpi")
    if not dpi:
        raise ValueError(f"{fmt}: the image carries no density, so its physical size is unknown (pass a Raster)")
    return Raster(im.convert("RGB"), float(dpi[0]), float(dpi[1]), None, fmt, [f"container density {dpi[0]:.2f}x{dpi[1]:.2f}"],
                  (float(dpi[0]), float(dpi[1])))


def pdf_facts(data: bytes) -> dict[str, Any]:
    """Document facts of a PDF: page count, per-page boxes (mm), which box defines the sheet, /Rotate, embedded images of page 1
    (pixels, effective dpi, stream filters)."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        pages = []
        for i in range(len(pdf)):
            pg = pdf[i]

            def box(getter) -> list[float] | None:
                try:
                    b = getter()
                except Exception:  # noqa: BLE001
                    return None
                return None if b is None else [round(v / 72.0 * MM, 3) for v in b]

            media = box(lambda: pg.get_mediabox(fallback_ok=True))
            crop = box(lambda: pg.get_cropbox(fallback_ok=False))
            trim = box(lambda: pg.get_trimbox(fallback_ok=False))
            bleed = box(lambda: pg.get_bleedbox(fallback_ok=False))
            art = box(lambda: pg.get_artbox(fallback_ok=False))
            ew, eh = pg.get_size()
            pages.append({
                "mediabox_mm": media, "cropbox_mm": crop, "trimbox_mm": trim, "bleedbox_mm": bleed, "artbox_mm": art,
                "effective_box": "CropBox" if crop else "MediaBox", "rotate": pg.get_rotation(),
                "size_mm": [round(ew / 72.0 * MM, 3), round(eh / 72.0 * MM, 3)],
            })
        images = []
        for o in pdf[0].get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]):
            md = o.get_metadata()
            images.append({"width": md.width, "height": md.height, "dpi_x": md.horizontal_dpi, "dpi_y": md.vertical_dpi,
                           "filters": list(o.get_filters())})
        try:
            subject = pdf.get_metadata_dict().get("Subject", "")
        except Exception:  # noqa: BLE001
            subject = ""
        return {"pages": len(pdf), "page_boxes": pages, "images": images, "subject": subject}
    finally:
        pdf.close()


def sequence_from_pdf(data: bytes) -> list[list[Any]] | None:
    """The ``[page, copy, side]`` list :func:`fakeprint.testpage.render_document` records in the PDF /Subject, or None."""
    try:
        d = json.loads(pdf_facts(data)["subject"])
        return d.get("sequence")
    except Exception:  # noqa: BLE001
        return None


def params_from_pdf(data: bytes) -> PageParams | None:
    """The parameters :func:`fakeprint.testpage.render_pdf` records in the PDF /Subject (survives a pass-through, not a conversion)."""
    try:
        subj = pdf_facts(data)["subject"]
        return PageParams.from_dict(json.loads(subj)) if subj.startswith("{") else None
    except Exception:  # noqa: BLE001
        return None


def parse_qr_payload(text: str) -> dict[str, Any]:
    """``key=value`` fields of a test page QR (separator ``|`` or ``;``); a ``params=`` field is base64url JSON of the
    :class:`PageParams`.  Foreign QR text parses to whatever fields it has (possibly none)."""
    out: dict[str, Any] = {}
    for part in text.replace(";", "|").split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    if "params" in out:
        try:
            out["params"] = json.loads(base64.urlsafe_b64decode(out["params"] + "=" * (-len(out["params"]) % 4)))
        except Exception:  # noqa: BLE001
            pass
    return out


# ------------------------------------------------------------------------------------------------ orientation

#: PIL transpose method names that FIX the defect, and what the defect is (received = defect(expected))
_FIXES = [
    (None, "upright", 0, False),
    ("ROTATE_90", "rotated 90 degrees clockwise", 90, False),
    ("ROTATE_180", "rotated 180 degrees", 180, False),
    ("ROTATE_270", "rotated 90 degrees counter-clockwise", 270, False),
    ("FLIP_LEFT_RIGHT", "mirrored left-right", 0, True),
    ("FLIP_TOP_BOTTOM", "flipped top-bottom", 180, True),
    ("TRANSPOSE", "transposed (mirrored along the top-left to bottom-right diagonal)", 90, True),
    ("TRANSVERSE", "transposed (mirrored along the top-right to bottom-left diagonal)", 270, True),
]


#: scales (of the true size) at which a page placed at the top-left corner is also tried by :func:`detect_orientation`
_PLACEMENT_SCALES = (1.0, 0.97, 1.03, 0.94, 1.06)


def _apply(img: Any, name: str | None) -> Any:
    if name is None:
        return img
    from PIL import Image

    return img.transpose(getattr(Image.Transpose, name))


def detect_orientation(raster_img: Any, ref_img: Any, cand_dpi: tuple[float, float], ref_dpi: float, size: int = 160) -> dict[str, Any]:
    """Which of the eight dihedral defects turns the expected page into the received one: the received image is transformed by each
    candidate fix, box-reduced and compared (mean absolute grey difference) with the expected page rendered through the same
    pipeline.  Two alignments are tried per candidate and the better kept: stretched to the expected page (a page scaled to fit)
    and at true physical size anchored at the top-left corner (a page shifted, cropped or placed on another sheet).  ``ambiguous``
    when the runner-up is within 15 % of the best (a blank or symmetric page)."""
    from PIL import Image, ImageChops, ImageOps, ImageStat

    def low(im: Any) -> tuple[Any, int]:
        g = im.convert("L")
        f = max(1, int(max(g.size) / size))
        return (g.reduce(f) if f > 1 else g), f

    ref_l, rf = low(ref_img)
    ref = ImageOps.autocontrast(ref_l)
    ref_pxmm = ref_dpi / MM / rf
    scores = []
    for name, label, rot, mir in _FIXES:
        swap = name in ("ROTATE_90", "ROTATE_270", "TRANSPOSE", "TRANSVERSE")
        dx, dy = (cand_dpi[1], cand_dpi[0]) if swap else cand_dpi
        cl, cf = low(_apply(raster_img, name))
        stretched = cl.resize(ref.size, resample=Image.Resampling.BOX)
        cands = [stretched]
        for f in _PLACEMENT_SCALES:  # the page at its true size at the top-left, and placed a few percent small / large (a fit-to-page, a zoom)
            phys = cl.resize((max(1, round(cl.width * ref_pxmm * f / (dx / MM / cf))), max(1, round(cl.height * ref_pxmm * f / (dy / MM / cf)))),
                             resample=Image.Resampling.BOX)
            canvas = Image.new("L", ref.size, 255)
            canvas.paste(phys, (0, 0))
            cands.append(canvas)
        d = min(ImageStat.Stat(ImageChops.difference(ImageOps.autocontrast(c), ref)).mean[0] for c in cands)
        scores.append((d, name, label, rot, mir))
    order = sorted(scores, key=lambda t: t[0])
    best, second = order[0], order[1]
    blank = ImageStat.Stat(ImageChops.difference(ref, ref.point(lambda v: 255))).mean[0]
    return {
        "fix": best[1], "verdict": best[2], "rotation_deg": best[3], "mirrored": best[4],
        "score": round(best[0], 3), "runner_up": round(second[0], 3), "blank_score": round(blank, 3),
        "ambiguous": second[0] < best[0] * 1.15 + 0.5,
        "match": best[0] < 0.55 * blank if blank > 0 else False,
    }


# ------------------------------------------------------------------------------------------------ the analysis


def _qr_in(raster_img: Any, A: Any, lay: dict[str, Any]) -> dict[str, Any] | None:
    from .qrdecode import decode_region

    qr = next((e for e in lay["elements"] if e["kind"] == "qr" and e.get("drawn", True)), None)
    if qr is None:
        return None
    x0, y0, x1, y1 = qr["rect_mm"]
    g = raster_img.convert("L")
    px = g.load()
    W, H = g.size

    def at(x: float, y: float) -> float:
        return px[min(max(int(x), 0), W - 1), min(max(int(y), 0), H - 1)]

    pad = 0.0  # the element rectangle already includes the quiet zone; neighbours must stay out of the dark bounding box
    cs = [A(x0 - pad, y0 - pad), A(x1 + pad, y1 + pad)]
    rect = (min(c[0] for c in cs), min(c[1] for c in cs), max(c[0] for c in cs), max(c[1] for c in cs))
    res = decode_region(at, rect)
    return {"ok": res.ok, "text": res.text, "version": res.version, "ecl": res.ecl, "ec_ok": res.ec_ok, "note": res.note,
            "fields": parse_qr_payload(res.text) if res.ok else {}}


def _reference(params: PageParams, layout: dict[str, Any] | None, dpi: float = ANALYSIS_DPI) -> tuple[TestPage, Any]:
    tp = render_pdf(params, layout)
    return tp, render_pdf_exact(tp.pdf, dpi).img


def _analysis_layout(lay: dict[str, Any], dpi: float) -> dict[str, Any]:
    """The layout with its nominal density set to the raster's: fakescan.diag3 plausibility-checks the fitted scale against
    ``dpi_effective`` (0.4 .. 2.5 x), and the adapter's layout is made at 100 .. 400 dpi whatever the received raster's density is
    (a 150 dpi raster against a 400 dpi layout is 0.375 x and would be refused as an "implausible affine")."""
    out = dict(lay)
    out["dpi_effective"] = float(dpi)
    out["px_per_mm"] = float(dpi) / MM
    return out


def _layout_revision(lay: dict[str, Any]) -> int:
    return int(lay.get("layout_revision", 0) or 0)


def marker_orientation(img: Any, lay: dict[str, Any], dpi: float, *, seed_scales: tuple[float, ...] = (1.0,)) -> dict[str, Any]:
    """Which of the eight dihedral defects the received page has, read from the layout's ArUco markers alone (four corners and four edge midpoints) (no glyph, no page
    correlation): the raster is transformed by each candidate fix and the markers are decoded where an upright page would have them
    (pure-Python detector, +-3 mm); the fix under which most markers decode as themselves (right id, rotation 0, not mirrored) wins.
    ``verdict`` uses the names of :func:`detect_orientation`; ``found`` is the number of markers read under that fix.  Needs
    a revision-1 layout (``available: False`` otherwise)."""
    from fakescan import diag
    from fakescan import diag_markers as dm

    marks = [e for e in lay["elements"] if e.get("kind") == "marker" and e.get("drawn", True) and e["id"].startswith("marker_")]  # not the ChArUco ones
    if len(marks) < 3 or _layout_revision(lay) < 1:
        return {"available": False, "verdict": None}
    small = dict(_analysis_layout(lay, dpi), elements=marks)
    scored = []
    for name, label, rot, mir in _FIXES:
        cand = _apply(img, name)
        ras = diag._Raster(cand)
        n = 0
        for f in seed_scales:
            k = dpi / MM * f
            det = dm.detect_markers(cand, ras, diag._Affine(k, 0.0, 0.0, 0.0, k, 0.0), small, detector="python")
            n = max(n, sum(1 for r in det["markers"].values()
                           if r.get("found") and r.get("decoded_id") == r.get("marker_id") and r.get("rotation_deg") == 0 and not r.get("mirrored")))
        scored.append((n, name, label, rot, mir))
    scored.sort(key=lambda t: -t[0])
    top = scored[0]
    return {"available": True, "fix": top[1], "verdict": top[2], "rotation_deg": top[3], "mirrored": top[4], "found": top[0],
            "runner_up_found": scored[1][0], "ambiguous": top[0] < 2 or scored[1][0] >= top[0]}


def _marker_figures(rep: dict[str, Any], d: dict[str, Any], px_per_mm: float) -> dict[str, Any]:
    """Copy the revision-1 ``markers`` / ``rulers`` of the fakescan report into ``rep``; return their headline figures for the summary."""
    out: dict[str, Any] = {}
    m, ru = d.get("markers"), d.get("rulers")
    if m:
        rep["markers"] = m
        out["markers_found"], out["markers_expected"] = m["n_found"], m["n_expected"]
        out["marker_detector"] = m["detector"]
        out["marker_orientation"] = (m.get("orientation") or {}).get("verdict")
        if "rms_corner_err_mm" in m:
            out["marker_rms_corner_err_mm"] = m["rms_corner_err_mm"]
            out["marker_rms_corner_err_px"] = m["rms_corner_err_mm"] * px_per_mm
        if "max_corner_err_mm" in m:
            out["marker_max_corner_err_mm"] = m["max_corner_err_mm"]
        if "homography" in m:
            out["homography_residual_rms_mm"] = m["homography"]["residual_rms_mm"]
            out["homography_residual_max_mm"] = m["homography"]["residual_max_mm"]
        if "affine" in m:
            out["marker_affine_dpi"] = [m["affine"]["dpi_x"], m["affine"]["dpi_y"]]
            out["marker_mirrored"] = m["affine"]["mirrored"]
    if ru:
        rep["rulers"] = ru
        out["ruler_dpi"] = {a: {u: v["dpi"] for u, v in units.items()} for a, units in ru.items()}
        out["ruler_length_error_mm"] = {a: {u: v["length_error_mm"] for u, v in units.items()} for a, units in ru.items()}
        out["ruler_baseline_mm"] = {a: {u: v["baseline_mm"] for u, v in units.items()} for a, units in ru.items()}
    return out


def analyze_raster(raster: Raster, params: PageParams, *, layout: dict[str, Any] | None = None, detail: str = "full",
                   facts: dict[str, Any] | None = None, page_index: int = 0, detector: str = "auto") -> dict[str, Any]:
    """Analyse one received page raster against the page ``params`` describe.  ``detail``: ``full`` or ``fast``.  ``detector``:
    fakescan's ArUco detector (``auto`` / ``python`` / ``cv2``).  Layout revision 1 pages add ``markers`` (ArUco corner / edge markers:
    corner error, homography residual in mm, flip / mirror verdict that does not use the orientation glyphs) and ``rulers`` (dpi per unit
    per axis and the length error over the ruler's baseline) to the report; the markers also fit the page when fewer than three
    registration crosses survive (``geometry.fit == "markers"``)."""
    from fakescan import diag

    lay = layout if layout is not None else layout_for(params)
    tp, ref = _reference(params, lay)
    exp_w, exp_h = params.size_mm()
    rep: dict[str, Any] = {"is_test_page": False, "reason": "", "format": raster.fmt, "page_index": page_index,
                           "expected_sheet_mm": [exp_w, exp_h], "layout_source": lay.get("source", "")}
    if facts:
        rep["document"] = facts
    ori = detect_orientation(raster.img, ref, (raster.dpi_x, raster.dpi_y), ANALYSIS_DPI)
    rep["orientation"] = ori
    rev1 = _layout_revision(lay) >= 1
    mo: dict[str, Any] | None = None
    if rev1 and (detail == "full" or not ori["match"]):
        # the ArUco markers read the RAW raster's fault (no glyph, no page correlation); they also rescue a page that is too cropped for the
        # whole-page comparison to recognise
        mo = marker_orientation(raster.img, lay, raster.dpi_x, seed_scales=(1.0, 0.94))
        rep["marker_orientation"] = mo
        if not ori["match"] and mo.get("available") and mo["found"] >= 2 and not mo["ambiguous"]:
            ori.update({"fix": mo["fix"], "verdict": mo["verdict"], "rotation_deg": mo["rotation_deg"], "mirrored": mo["mirrored"],
                        "match": True, "ambiguous": False, "source": "markers"})
    if not ori["match"]:
        rep["reason"] = (f"not a fakeprint test page (or not this page): best of 8 orientations differs by {ori['score']} grey levels "
                         f"from the expected page (a blank page would give {ori['blank_score']})")
        return rep
    # canonical (upright) raster; a 90-degree fix swaps the axes' densities
    img = _apply(raster.img, ori["fix"])
    swap = ori["fix"] in ("ROTATE_90", "ROTATE_270", "TRANSPOSE", "TRANSVERSE")
    dpi_x, dpi_y = (raster.dpi_y, raster.dpi_x) if swap else (raster.dpi_x, raster.dpi_y)
    nominal = dpi_x  # the analysis raster is square-pixel by construction
    sheet = (img.width / dpi_x * MM, img.height / dpi_y * MM)
    rep["received_sheet_mm"] = [round(sheet[0], 3), round(sheet[1], 3)]
    rep["sheet_size_ok"] = abs(sheet[0] - exp_w) < 1.0 and abs(sheet[1] - exp_h) < 1.0
    s0 = dpi_x / MM
    # the diag analyzer works on a raster that shows the layout box; our raster is the sheet, the layout origin is the sheet origin
    rw, rh = lay.get("raster_mm") or lay.get("sheet_mm") or [exp_w, exp_h]
    # the cross search is local, so try a few initial scales (1, the sheet-size ratio, a few usual fit-to-page factors) and keep the
    # fit with the smallest residual; stop at the first sub-half-pixel one
    hints = list(dict.fromkeys(round(h, 4) for h in (1.0, sheet[0] / exp_w, sheet[1] / exp_h, 0.94, 0.97, 1.03)))
    alay = _analysis_layout(lay, nominal)
    best = None
    for hint in hints:
        a = (s0 * hint, 0.0, 0.0, 0.0, s0 * hint, 0.0)
        r = diag.analyze_diag_page(img, alay, scan_dpi=nominal, init=a, detector=detector, **({} if rev1 else {"marker_fallback": False}))
        g = r.get("geometry") or {}
        fit = g.get("fit") if "affine" in g else None  # "crosses" | "markers" | None (failed)
        res = g.get("fit_residual_px_rms")
        if res is None and fit == "markers":  # the marker fit has no cross residual: the homography residual of the marker corners, in px
            res = ((r.get("markers") or {}).get("homography") or {}).get("residual_rms_mm", 9.0) * nominal / MM
        if fit == "crosses" and g.get("crosses_found", 0) < 3:
            fit = None
        key = (fit is None, fit != "crosses", res if res is not None else 1e9)
        if best is None or key < best[0]:
            best = (key, r)
        if fit == "crosses" and res is not None and res < 0.5:
            break
    d = best[1]
    geo = d.get("geometry", {})
    rep["geometry"] = geo
    if not rev1:
        # LEGACY (layout revision 0, an extension older than fakescan 196ddc0): fakescan.diag3 accepted any local maximum as a cross, and a
        # fit through three points has residual ~0: crosses scoring far below the best one are clutter found where a cut-off corner used
        # to be, so they count as not found (and a fit needs three real ones).  Revision 1 does this itself (and falls back to the markers).
        weak = _weak_crosses(geo)
        if weak:
            geo["weak_crosses"] = weak
            if len(geo.get("registration", {})) - len(weak) < 3:
                for k in ("scale_x_vs_nominal", "scale_y_vs_nominal"):
                    geo.pop(k, None)
    if d.get("summary", {}).get("fit") == "failed" or "scale_x_vs_nominal" not in geo or geo.get("fit_residual_px_rms", 0) > 2.5:
        rep["is_test_page"] = True
        rep["reason"] = ("orientation matched but fewer than 3 registration crosses (and too few ArUco markers) could be fitted (are the corners cut off?)"
                         if "scale_x_vs_nominal" not in geo else
                         f"orientation matched but the registration fit is poor (residual {geo['fit_residual_px_rms']:.1f} px): measurements withheld")
        rep["summary"] = {"verdict_orientation": ori["verdict"], "rotation_deg_90s": ori["rotation_deg"], "mirrored": ori["mirrored"],
                          "sheet_mm": rep["received_sheet_mm"], "sheet_size_ok": rep["sheet_size_ok"], "fit": "failed"}
        return rep
    rep["is_test_page"] = True
    summ = dict(d["summary"])
    v3 = lay.get("layout_version") == 3 or (lay.get("schema") or "").startswith("fakescan.diag.layout/3")
    if v3:  # fakescan.diag3 names the scale "vs nominal": the same quantity under the v2 analyzer's names
        summ.setdefault("scale_x", summ["scale_x_vs_nominal"])
        summ.setdefault("scale_y", summ["scale_y_vs_nominal"])
        summ.setdefault("clipped_edges", [])
    off = geo["origin_offset_mm"]
    extent = [off[0] + rw * summ["scale_x"], off[1] + rh * summ["scale_y"]]
    over = [max(0.0, extent[0] - sheet[0]), max(0.0, extent[1] - sheet[1])]
    margins = {"left": round(off[0], 3), "top": round(off[1], 3),
               "right": round(sheet[0] - extent[0], 3), "bottom": round(sheet[1] - extent[1], 3)}
    clipped = list(summ.get("clipped_edges", []))
    if v3 and params.colour == "mono" and not rev1:
        # LEGACY (layout revision 0; revision 1 scores the frame by luminance on gray pages): fakescan.diag3 finds the frame as "blue minus red", which a gray page does not have: its per-edge result would call every edge
        # clipped.  Not assessable here; the margins and the layout-beyond-sheet figures below still are.
        clipped = []
        rep.setdefault("notes", []).append("frame clipping not assessed on a mono page (fakescan.diag3 frame detector needs colour)")
    if lay.get("fallback"):
        # v2 draws its page_border at the edge of its own raster, which the fallback places inside the sheet: a pdfium-resampled
        # 1 px hairline there is not evidence about the sheet edge, so it is not reported (the margins below are)
        clipped = [c for c in clipped if not c.startswith("page_border:")]
    for side, v in margins.items():
        if v < -0.3:
            clipped.append(f"layout:{side} ({-v:.1f} mm outside the sheet)")
    qr = _qr_in(img, _aff(geo), lay)
    rep["qr"] = qr
    summary = {
        "verdict_orientation": ori["verdict"], "rotation_deg_90s": ori["rotation_deg"], "mirrored": ori["mirrored"],
        "scale_x": summ["scale_x"], "scale_y": summ["scale_y"], "skew_rotation_deg": summ["rotation_deg"],
        "margins_mm": margins, "clipped_edges": clipped, "layout_beyond_sheet_mm": [round(o, 3) for o in over],
        "sheet_mm": rep["received_sheet_mm"], "sheet_size_ok": rep["sheet_size_ok"],
        "qr_text": (qr or {}).get("text"), "qr_ok": bool(qr and qr["ok"]),
        "fit_residual_px": round(geo.get("fit_residual_px_rms", 0.0), 3),
        "fit": geo.get("fit", "crosses"),
    }
    summary.update(_marker_figures(rep, d, nominal / MM))
    if mo is not None:
        if mo.get("available"):
            summary["marker_orientation_check"] = {"verdict": mo["verdict"], "found": mo["found"], "agrees": mo.get("fix") == ori["fix"]}
    fields = (qr or {}).get("fields") or {}
    if v3:
        _read_v3_stamps(summary, d, fields)
    for k_out, k_in in (() if v3 else (("page", "page"), ("pages", "pages"), ("copy", "copy"), ("copies", "copies"), ("side", "side"), ("job", "job"))):
        if k_in in fields:
            summary[k_out] = fields[k_in]
    if isinstance(fields.get("params"), dict):
        pp = fields["params"]
        summary.update({"page": pp.get("page"), "pages": pp.get("pages"), "copy": pp.get("copy"), "copies": pp.get("copies"),
                        "side": pp.get("side"), "job": pp.get("job")})
    for k in ("colorchecker_mean_delta_e76", "channel_order", "flat_field_max_stdev", "flat_field_max_row_range",
              "ruler_metric_pitch_error_pct", "ruler_inch_pitch_error_pct", "margin_errors_mm", "orientation"):
        if v3 and k in summ:
            summary["glyph_orientation" if k == "orientation" else k] = summ[k]
    for k in ("hue_err_deg_max_abs", "shadows_clipped_steps", "highlights_clipped_steps", "measure_bar_length_error_pct",
              "res_lines_v_last_resolvable_period_mm", "res_lines_h_last_resolvable_period_mm"):
        if k in summ:
            summary[k] = summ[k]
    rep["summary"] = summary
    if detail == "full":
        rep["elements"] = d.get("elements", {})
    return rep


def _weak_crosses(geo: dict[str, Any]) -> list[str]:
    """Registration crosses (v3 geometry) whose detection score is below 40 % of the best one's."""
    reg = geo.get("registration") or {}
    if not reg:
        return []
    top = max(v.get("score", 0.0) for v in reg.values())
    return [k for k, v in reg.items() if v.get("score", 0.0) < 0.4 * top]


def _read_v3_stamps(summary: dict[str, Any], d: dict[str, Any], fields: dict[str, Any]) -> None:
    """Page / copy / side / job of a layout-v3 page as READ from the page (not as expected): the big page-number digits (``page``, 1-based)
    and the order bars (``page_bars``) and the side marker (``side``, ``front`` / ``back``) come from fakescan.diag3's ``page_order``
    element; ``pages`` (``of=``), ``copy`` and ``job`` come from the decoded QR payload, if it decoded.  ``page_order_ok`` says whether
    the digits, the bars and the side marker agree with the layout the analysis was run against (the EXPECTED page)."""
    el = d.get("elements", {})
    po = el.get("page_order") or {}
    pn, bars, sm = po.get("page_number") or {}, po.get("order_bars") or {}, po.get("side_marker") or {}
    read = pn.get("read")
    summary["page"] = int(read) if isinstance(read, str) and read.isdigit() else (read if isinstance(read, int) else None)
    summary["page_bars"] = bars.get("filled_found")
    summary["side"] = sm.get("side_read")
    summary["page_order_ok"] = bool(pn.get("ok") and bars.get("ok") and sm.get("ok"))
    summary["glyph_orientation"] = (el.get("orientation") or {}).get("verdict")
    for k_out, k_in in (("pages", "of"), ("copy", "copy")):
        v = fields.get(k_in)
        if isinstance(v, str) and v.isdigit():
            summary[k_out] = int(v)
    if fields.get("job"):
        summary["job"] = fields["job"]


class _AffineView:
    def __init__(self, m: list[float]) -> None:
        self.a, self.b, self.c, self.d, self.e, self.f = m

    def __call__(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.b * y + self.c, self.d * x + self.e * y + self.f


def _aff(geo: dict[str, Any]) -> _AffineView:
    return _AffineView(geo["affine"])


def analyze_document(data: bytes, fmt: str = "application/pdf", params: PageParams | None = None, *, dpi: float = ANALYSIS_DPI,
                     pages: str | int = "first", detail: str = "full", raster: Raster | None = None) -> dict[str, Any]:
    """Analyse a received document.  ``fmt``: ``application/pdf``, ``image/png``, ``image/jpeg``, ``image/tiff`` (anything else: pass
    ``raster``, a :class:`Raster` you decoded).  ``params``: the parameters the page was generated from (default: recovered from the
    PDF /Subject).  ``pages``: ``"first"``, ``"all"`` or a 0-based index; with several pages ``pages`` in the result lists each one's
    analysis (stamps in document order: the collate / duplex evidence)."""
    fmt = fmt.lower()
    out: dict[str, Any] = {"format": fmt, "bytes": len(data), "is_test_page": False}
    facts: dict[str, Any] | None = None
    n_pages = 1
    if fmt == "application/pdf":
        try:
            facts = pdf_facts(data)
        except Exception as e:  # noqa: BLE001
            out["reason"] = f"unreadable PDF: {type(e).__name__}: {e}"
            return out
        n_pages = facts["pages"]
        out["document"] = facts
        params = params or params_from_pdf(data)
    if params is None:
        out["reason"] = "not a fakeprint test page (no parameters given and none recorded in the document)"
        if raster is None and fmt in ("image/png", "image/jpeg", "image/tiff"):
            try:
                r = image_raster(fmt, data)
                out["sheet_mm"] = [round(r.img.width / r.dpi_x * MM, 2), round(r.img.height / r.dpi_y * MM, 2)]
            except Exception as e:  # noqa: BLE001
                out["reason"] += f"; {e}"
        elif facts:
            out["sheet_mm"] = facts["page_boxes"][0]["size_mm"]
        return out
    idx = range(n_pages) if pages == "all" else [pages if isinstance(pages, int) else 0]
    seq = sequence_from_pdf(data) if fmt == "application/pdf" else None
    results = []
    for i in idx:
        if raster is not None:
            r = raster
        elif fmt == "application/pdf":
            r = render_pdf_exact(data, dpi, page_index=i)
        elif fmt in ("image/png", "image/jpeg", "image/tiff"):
            r = image_raster(fmt, data)
        else:
            raise ValueError(f"cannot rasterise {fmt}: pass raster=")
        p = params
        if seq and i < len(seq):
            p = params.replace(page=int(seq[i][0]), copy=int(seq[i][1]), side=str(seq[i][2]))
        # the layout is per page: the page-number digits, bars, side marker and stamps differ between the pages of a document
        results.append(analyze_raster(r, p, layout=None, detail=detail if i == next(iter(idx)) else "fast", facts=facts if i == next(iter(idx)) else None, page_index=i))
    first = results[0]
    first["document_pages"] = n_pages
    if len(results) > 1:
        first["pages"] = [{"page_index": x["page_index"], "is_test_page": x["is_test_page"], **(x.get("summary") or {})} for x in results]
    return first


def brief(rep: dict[str, Any]) -> dict[str, Any]:
    """The one-screen summary (what ``Client.job_analysis`` returns under ``summary``): scale, rotation / flip verdict, clipped
    edges, page / copy index, QR text; or the reason it is not a test page."""
    if not rep.get("is_test_page"):
        return {"is_test_page": False, "reason": rep.get("reason", ""), "sheet_mm": rep.get("sheet_mm")}
    s = rep.get("summary") or {}
    keys = ("verdict_orientation", "scale_x", "scale_y", "skew_rotation_deg", "clipped_edges", "margins_mm", "sheet_mm", "sheet_size_ok",
            "page", "pages", "copy", "copies", "side", "job", "qr_text", "page_bars", "page_order_ok", "glyph_orientation",
            "fit", "markers_found", "markers_expected", "marker_orientation", "homography_residual_rms_mm", "ruler_dpi")
    out: dict[str, Any] = {"is_test_page": True, **{k: s[k] for k in keys if k in s}}
    if rep.get("reason"):
        out["reason"] = rep["reason"]
    return out
