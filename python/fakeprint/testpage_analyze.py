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
        phys = cl.resize((max(1, round(cl.width * ref_pxmm / (dx / MM / cf))), max(1, round(cl.height * ref_pxmm / (dy / MM / cf)))),
                         resample=Image.Resampling.BOX)
        canvas = Image.new("L", ref.size, 255)
        canvas.paste(phys, (0, 0))
        d = min(ImageStat.Stat(ImageChops.difference(ImageOps.autocontrast(c), ref)).mean[0] for c in (stretched, canvas))
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


def analyze_raster(raster: Raster, params: PageParams, *, layout: dict[str, Any] | None = None, detail: str = "full",
                   facts: dict[str, Any] | None = None, page_index: int = 0) -> dict[str, Any]:
    """Analyse one received page raster against the page ``params`` describe.  ``detail``: ``full`` or ``fast``."""
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
    best = None
    for hint in hints:
        a = (s0 * hint, 0.0, 0.0, 0.0, s0 * hint, 0.0)
        r = diag.analyze_diag_page(img, lay, scan_dpi=nominal, init=a)
        res = (r.get("geometry") or {}).get("fit_residual_px_rms")
        n = (r.get("geometry") or {}).get("crosses_found", 0)
        key = (n < 3, res if res is not None else 1e9)
        if best is None or key < best[0]:
            best = (key, r)
        if n >= 3 and res is not None and res < 0.5:
            break
    d = best[1]
    geo = d.get("geometry", {})
    rep["geometry"] = geo
    if d.get("summary", {}).get("fit") == "failed" or "scale_x_vs_nominal" not in geo or geo.get("fit_residual_px_rms", 0) > 2.5:
        rep["is_test_page"] = True
        rep["reason"] = ("orientation matched but fewer than 3 registration crosses could be fitted (are the corners cut off?)"
                         if "scale_x_vs_nominal" not in geo else
                         f"orientation matched but the registration fit is poor (residual {geo['fit_residual_px_rms']:.1f} px): measurements withheld")
        rep["summary"] = {"verdict_orientation": ori["verdict"], "rotation_deg_90s": ori["rotation_deg"], "mirrored": ori["mirrored"],
                          "sheet_mm": rep["received_sheet_mm"], "sheet_size_ok": rep["sheet_size_ok"], "fit": "failed"}
        return rep
    rep["is_test_page"] = True
    summ = dict(d["summary"])
    off = geo["origin_offset_mm"]
    extent = [off[0] + rw * summ["scale_x"], off[1] + rh * summ["scale_y"]]
    over = [max(0.0, extent[0] - sheet[0]), max(0.0, extent[1] - sheet[1])]
    margins = {"left": round(off[0], 3), "top": round(off[1], 3),
               "right": round(sheet[0] - extent[0], 3), "bottom": round(sheet[1] - extent[1], 3)}
    clipped = list(summ.get("clipped_edges", []))
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
    }
    fields = (qr or {}).get("fields") or {}
    for k_out, k_in in (("page", "page"), ("pages", "pages"), ("copy", "copy"), ("copies", "copies"), ("side", "side"), ("job", "job")):
        if k_in in fields:
            summary[k_out] = fields[k_in]
    if isinstance(fields.get("params"), dict):
        pp = fields["params"]
        summary.update({"page": pp.get("page"), "pages": pp.get("pages"), "copy": pp.get("copy"), "copies": pp.get("copies"),
                        "side": pp.get("side"), "job": pp.get("job")})
    for k in ("hue_err_deg_max_abs", "shadows_clipped_steps", "highlights_clipped_steps", "measure_bar_length_error_pct",
              "res_lines_v_last_resolvable_period_mm", "res_lines_h_last_resolvable_period_mm"):
        if k in summ:
            summary[k] = summ[k]
    rep["summary"] = summary
    if detail == "full":
        rep["elements"] = d.get("elements", {})
    return rep


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
    lay = layout_for(params)
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
        results.append(analyze_raster(r, p, layout=lay, detail=detail if i == next(iter(idx)) else "fast", facts=facts if i == next(iter(idx)) else None, page_index=i))
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
            "page", "pages", "copy", "copies", "side", "job", "qr_text")
    out: dict[str, Any] = {"is_test_page": True, **{k: s[k] for k in keys if k in s}}
    if rep.get("reason"):
        out["reason"] = rep["reason"]
    return out
