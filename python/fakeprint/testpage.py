"""The fakeprint print test page: parameters, a size-parametric layout adapter and a deterministic vector PDF/PNG writer.

DESIGN (owner requirement): no page fixtures anywhere.  Every byte of the test page is produced from
:class:`PageParams` (paper name or custom size, orientation, printable-area margins, bleed, dpi, colour mode, page / copy /
total, duplex side, job tag) by this module, and the page SCALES to the sheet: the layout is in millimetres and an element that
does not fit is recorded ``drawn: false`` (or with a ``scale``) by whoever produces the layout.

ONE layout engine.  The layout (what is on the page, where, with the ground truth the analyzer checks) comes from the
fakescan generator, not from here.  This module owns only

* :class:`PageParams` / :func:`paper_size` - the parameters and the paper registry (A0-A6, ISO B, JIS B, Letter, Legal, Tabloid,
  Executive, Statement, custom ``WxH`` mm);
* :func:`layout_for` - THE ADAPTER (the single place that knows where the layout comes from; see its docstring for the API
  expected from fakescan and for the clearly marked fallback);
* :class:`PdfWriter` / :func:`render_pdf` - a pure-Python writer that places the layout's vector primitives by their
  millimetre coordinates at ANY sheet size (exact MediaBox, no scaling, vector stays vector, deterministic bytes);
* :func:`render_png` - the same page rasterised with pdfium at a stated dpi (PNG carries the density).

Primitive schema (what :class:`PdfWriter` draws; coordinates in mm, origin top-left of the SHEET, x right, y down; colours
are ``[r, g, b]`` 0..255; every key but the shape is optional)::

    {"op": "line",   "x1":, "y1":, "x2":, "y2":, "w": 0.2, "dash": [on, off], "rgb": [0, 0, 0]}
    {"op": "rect",   "x":, "y":, "w":, "h":, "fill": rgb, "stroke": rgb, "sw": 0.2, "dash": [on, off]}
    {"op": "rects",  "rects": [[x, y, w, h], ...], "fill": rgb}      (ONE path, non-zero winding: touching or overlapping rects are
                     a union, painted without anti-aliasing seams; :func:`coalesce_rects` builds these from runs of ``rect`` ops)
    {"op": "poly",   "pts": [[x, y], ...], "close": true, "fill": rgb, "stroke": rgb, "sw": 0.2}
    {"op": "circle", "cx":, "cy":, "r":, "fill": rgb, "stroke": rgb, "sw": 0.2}
    {"op": "text",   "x":, "y":, "size_pt": 10, "text": "...", "font": "sans|serif|mono", "bold": false,
                     "anchor": "start|middle|end", "rgb": rgb, "rotate_deg": 0}      (y = baseline)
    {"op": "image",  "x":, "y":, "w":, "h":, "px_w":, "px_h":, "mode": "gray|rgb", "data_b64": "..."}   (raw 8-bit samples)
    {"op": "qr",     "x":, "y":, "size":, "modules": [[0, 1, ...], ...], "quiet": 0}   (1 = dark; square of ``size`` mm)

An element is ``{"id", "kind", "drawn", "rect_mm", "expected": {...}, "primitives": [...]}``.  ``colour`` mono pages are written
in DeviceGray (luma) - nothing else changes.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import zlib
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

MM = 25.4
PT = 72.0

#: paper name -> (width mm, height mm) in PORTRAIT
PAPERS: dict[str, tuple[float, float]] = {
    "a0": (841.0, 1189.0), "a1": (594.0, 841.0), "a2": (420.0, 594.0), "a3": (297.0, 420.0), "a4": (210.0, 297.0),
    "a5": (148.0, 210.0), "a6": (105.0, 148.0),
    "isob3": (353.0, 500.0), "isob4": (250.0, 353.0), "isob5": (176.0, 250.0), "isob6": (125.0, 176.0),
    "jisb4": (257.0, 364.0), "jisb5": (182.0, 257.0), "jisb6": (128.0, 182.0),
    "letter": (215.9, 279.4), "legal": (215.9, 355.6), "tabloid": (279.4, 431.8), "executive": (184.15, 266.7),
    "statement": (139.7, 215.9),
}
_ALIASES = {"b3": "isob3", "b4": "isob4", "b5": "isob5", "b6": "isob6", "ledger": "tabloid", "us letter": "letter",
            "usletter": "letter", "naletter": "letter", "nalegal": "legal", "ansib": "tabloid", "11x17": "tabloid"}


def paper_size(name: str) -> tuple[float, float]:
    """Portrait ``(w, h)`` mm for a paper name (``A4``, ``Letter``, ``ISO B5``, ``JIS-B4``, ...) or a custom ``"210x297"``
    (also ``"210 x 297 mm"``).  Raises ``ValueError`` for anything else."""
    key = re.sub(r"[^a-z0-9.x]", "", name.strip().lower().replace("iso_", "iso").replace("iso-", "iso").replace("iso ", "iso"))
    key = _ALIASES.get(key, key)
    if key in PAPERS:
        return PAPERS[key]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(?:mm)?", key)
    if m:
        w, h = float(m.group(1)), float(m.group(2))
        if w <= 0 or h <= 0:
            raise ValueError(f"bad custom size {name!r}")
        return (w, h)
    raise ValueError(f"unknown paper {name!r}; known: {', '.join(sorted(PAPERS))} or WxH in mm")


@dataclass(frozen=True)
class PageParams:
    """Everything that determines a test page.  Frozen and JSON-able: the QR on the page carries it, so a decoder can regenerate
    the expected page."""

    paper: str = "a4"  # a name from PAPERS, or "WxH" mm; ``width_mm``/``height_mm`` override
    width_mm: float | None = None
    height_mm: float | None = None
    orientation: str = "portrait"  # portrait | landscape
    margin_mm: float = 6.35  # printable-area inset (the frame the page draws): 1/4 in is conservative for office printers
    bleed_mm: float = 0.0
    dpi: int = 150  # raster (PNG) density; the PDF is vector and ignores it
    colour: str = "colour"  # colour | mono
    page: int = 1
    pages: int = 1
    copy: int = 1
    copies: int = 1
    side: str = "front"  # front | back (duplex)
    job: str = "fakeprint"  # job tag, max 16 chars [A-Za-z0-9_.-]
    seed: int = 0  # only for seeded pseudo-random elements (none of the vector ones)

    def __post_init__(self) -> None:
        if self.orientation not in ("portrait", "landscape"):
            raise ValueError("orientation must be portrait or landscape")
        if self.colour not in ("colour", "mono"):
            raise ValueError("colour must be colour or mono")
        if self.side not in ("front", "back"):
            raise ValueError("side must be front or back")
        if not 1 <= self.page <= self.pages or not 1 <= self.copy <= self.copies:
            raise ValueError("page/copy out of range")
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,16}", self.job):
            raise ValueError("job tag must be 1..16 chars of [A-Za-z0-9_.-]")
        self.size_mm()  # validates the paper

    def size_mm(self) -> tuple[float, float]:
        """Sheet ``(w, h)`` mm in the requested orientation."""
        if self.width_mm and self.height_mm:
            w, h = float(self.width_mm), float(self.height_mm)
        else:
            w, h = paper_size(self.paper)
        short, long = min(w, h), max(w, h)
        return (long, short) if self.orientation == "landscape" else (short, long)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PageParams:
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def replace(self, **kw: Any) -> PageParams:
        return PageParams.from_dict({**self.to_dict(), **kw})


class LayoutUnavailable(RuntimeError):
    """No layout source can produce a page for these parameters (see :func:`layout_for`)."""


# ------------------------------------------------------------------------------------------------ the adapter


def v2_layout(dpi: int = 150) -> dict[str, Any]:
    """The fixed-size layout v2 from the installed fakescan, whichever signature its ``diag`` has: ``diag_page_layout(dpi, version)``
    (version 2 asked explicitly because fakescan's default is the parametric v3) or the bare native call of an older extension."""
    from fakescan import diag

    try:
        return diag.diag_page_layout(dpi, 2)
    except TypeError:  # an extension older than the Python module: no params argument
        from fakescan import _native

        return json.loads(_native.diag_layout_json(dpi, 2))


def v2_render(dpi: int = 150):
    """The v2 page as an RGB Pillow image (same compatibility rule as :func:`v2_layout`)."""
    from fakescan import diag

    try:
        return diag.render_diag_page(dpi, 0, 2)
    except TypeError:
        from PIL import Image

        from fakescan import _native

        w, h, data = _native.diag_render_rgb(dpi, 0, 2)
        return Image.frombytes("RGB", (w, h), bytes(data))


#: raster density of the fakescan render the adapter reads text cells and raster-only elements from (capped by ``MAX_LAYOUT_PIXELS``)
LAYOUT_DPI = 400
#: pixel budget of that render (an A3 sheet at 400 dpi is 31 MP; A0 would be 250 MP, so the density drops with the sheet size)
MAX_LAYOUT_PIXELS = 36_000_000
#: raster-only primitive ops (``vector: false`` in fakescan): they are cropped from the render and embedded as images
_RASTER_OPS = ("ramp", "hue_wheel", "siemens_star", "zone_plate", "halftone", "qr")


def layout_dpi(w_mm: float, h_mm: float) -> int:
    """The density of the fakescan render for a ``w_mm`` x ``h_mm`` sheet."""
    cap = math.sqrt(MAX_LAYOUT_PIXELS / (w_mm * h_mm)) * MM
    return max(100, min(LAYOUT_DPI, int(cap)))


@lru_cache(maxsize=1)
def v3_available() -> bool:
    """True when the installed fakescan has the parametric layout v3 (Python module AND native extension)."""
    try:
        from fakescan import diag

        diag.diag_page_layout(50, paper="a6")
        return diag.diag_page_primitives is not None
    except Exception:  # noqa: BLE001  (ImportError, AttributeError, TypeError of an old extension...)
        return False


def v3_kwargs(params: PageParams) -> dict[str, Any]:
    """:class:`PageParams` as the keyword parameters of ``fakescan.diag.diag_page_layout`` (layout v3).  Mapping: ``page`` (1-based here) is
    fakescan's 0-based page index, ``pages`` its ``total``, ``copy`` is stamped as given, ``job`` is upper-cased to fakescan's alphabet,
    ``colour="mono"`` is fakescan's ``gray``.  The sheet is passed as fakescan's paper name when it knows that size, else as ``width_mm`` x
    ``height_mm`` (short x long); plus ``orientation``."""
    w, h = params.size_mm()
    short, long = min(w, h), max(w, h)
    sheet: dict[str, Any] = {"width_mm": short, "height_mm": long}
    if not (params.width_mm and params.height_mm):  # a named paper: let fakescan name it on the page (legend, QR) when it knows the size
        from fakescan import diag

        key = re.sub(r"[^a-z0-9]", "", params.paper.lower())
        name = {"isob4": "B4", "isob5": "B5", "jisb4": "JIS-B4", "jisb5": "JIS-B5", "b4": "B4", "b5": "B5"}.get(key, key.upper())
        known = diag.diag_paper_sizes().get(name)
        if known and abs(min(known) - short) < 0.1 and abs(max(known) - long) < 0.1:
            sheet = {"paper": name}
    kw: dict[str, Any] = {
        **sheet, "orientation": params.orientation, "margin_mm": params.margin_mm,
        "page": params.page - 1, "copy": params.copy, "total": params.pages, "side": params.side,
        "job": re.sub(r"[^A-Z0-9_\-]", "-", params.job.upper()), "colour": "gray" if params.colour == "mono" else "rgb",
    }
    if params.bleed_mm:
        kw["bleed_mm"] = params.bleed_mm
    return kw


def _v3_pdf_primitives(lay: dict[str, Any], img: Any, mono: bool) -> list[dict[str, Any]]:
    """Translate fakescan's vector primitive list (``diag_page_primitives``: millimetres, paint order) to the writer's schema.

    * ``rect polygon line circle`` map one to one; ``grating`` and ``ticks`` expand to the rectangles fakescan paints for them;
    * ``text`` is fakescan's own 5x7 pixel font: the cell grid of each string is READ from the render at the cell centres (ink = nearer to the
      primitive's colour than to the background) and written as rectangles, so the glyphs are exact, vector, mirrored where fakescan mirrors
      them, and no copy of the font table lives here;
    * raster-only ops (``ramp hue_wheel siemens_star zone_plate halftone qr``) are cropped from the render at its density and embedded as
      images at their exact physical rectangle."""
    from fakescan import diag

    k = lay["px_per_mm"]
    px = img.load()
    W, H = img.size
    out: list[dict[str, Any]] = []

    def at(x_mm: float, y_mm: float) -> tuple[int, int, int]:
        v = px[min(max(int(x_mm * k), 0), W - 1), min(max(int(y_mm * k), 0), H - 1)]
        return v if isinstance(v, tuple) else (v, v, v)

    def merged(cells: list[tuple[int, int]], x0: float, y0: float, u: float, rgb: list[int]) -> list[dict[str, Any]]:
        """Horizontal runs of ``(col, row)`` cells as rect primitives."""
        rows: dict[int, list[int]] = {}
        for c, r in cells:
            rows.setdefault(r, []).append(c)
        res = []
        for r in sorted(rows):
            cols = sorted(rows[r])
            i = 0
            while i < len(cols):
                j = i
                while j + 1 < len(cols) and cols[j + 1] == cols[j] + 1:
                    j += 1
                res.append({"op": "rect", "x": x0 + cols[i] * u, "y": y0 + r * u, "w": (cols[j] - cols[i] + 1) * u, "h": u, "fill": rgb})
                i = j + 1
        return res

    for p in diag.diag_page_primitives(lay):
        op, eid = p["op"], p.get("id", "")
        if op == "rect":
            x0, y0, x1, y1 = p["rect_mm"]
            out.append({"op": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "fill": p["rgb"], "id": eid})
        elif op == "polygon":
            out.append({"op": "poly", "pts": p["points_mm"], "fill": p["rgb"], "id": eid})
        elif op == "line":
            d = p.get("dash_mm")
            (xa, ya), (xb, yb) = p["from_mm"], p["to_mm"]
            out.append({"op": "line", "x1": xa, "y1": ya, "x2": xb, "y2": yb, "w": p["width_mm"], "rgb": p["rgb"], "id": eid,
                        **({"dash": d} if d else {})})
        elif op == "circle":
            st = p.get("stroke")
            out.append({"op": "circle", "cx": p["centre_mm"][0], "cy": p["centre_mm"][1], "r": p["radius_mm"], "id": eid,
                        **({"fill": p["fill"]} if p.get("fill") else {}),
                        **({"stroke": st["rgb"], "sw": st["width_mm"]} if st else {})})
        elif op == "grating":
            x0, y0, x1, y1 = p["rect_mm"]
            per = p["period_mm"]
            a0, a1 = (x0, x1) if p["vertical_lines"] else (y0, y1)
            s = a0
            while s < a1 - 1e-9:
                e = min(s + per / 2.0, a1)
                r = ({"op": "rect", "x": s, "y": y0, "w": e - s, "h": y1 - y0} if p["vertical_lines"]
                     else {"op": "rect", "x": x0, "y": s, "w": x1 - x0, "h": e - s})
                out.append({**r, "fill": p["rgb"], "id": eid})
                s += per
        elif op == "ticks":
            ox, oy = p["origin_mm"]
            w = p["width_mm"]
            tiers = p["tiers"]
            for i in range(p["count"] + 1):
                ln = next((t[1] for t in reversed(tiers) if i % t[0] == 0), 0.0)
                if ln <= 0:
                    continue
                q = i * p["pitch_mm"]
                if p["horizontal"]:
                    y0 = oy if p["down"] else oy - ln
                    out.append({"op": "rect", "x": ox + q - w / 2, "y": y0, "w": w, "h": ln, "fill": p["rgb"], "id": eid})
                else:
                    x0 = ox if p["down"] else ox - ln
                    out.append({"op": "rect", "x": x0, "y": oy + q - w / 2, "w": ln, "h": w, "fill": p["rgb"], "id": eid})
        elif op == "text":
            u = 0.72 * p["pt"] * MM / 72.0 / 7.0
            n = len(p["text"])
            total = max(n, 1) * 6 - 1
            x, y = p["x_mm"], p["y_mm"] - 2 * u  # row 0 of the 10-row extended glyph (two accent rows above the cap top)
            ink = p["rgb"]
            # background: the always-blank gutter column after each character (a one-character string: the cell left of the string)
            gut = [at(x + (6 * i + 5.5) * u, y + (r + 0.5) * u) for i in range(n - 1) for r in range(10)] or [at(x - 0.5 * u, y + 4.5 * u)]
            bg = max(set(gut), key=gut.count)

            def d2(c: tuple[int, int, int], ref: Any) -> int:
                return sum((c[i] - ref[i]) ** 2 for i in range(3))

            cells = []
            for r in range(10):
                for c in range(total):
                    if c % 6 == 5:
                        continue
                    v = at(x + (c + 0.5) * u, y + (r + 0.5) * u)
                    if d2(v, ink) < d2(v, bg):
                        cells.append((c, r))
            for q in merged(cells, x, y, u, ink):
                q["id"] = eid
                out.append(q)
        elif op in _RASTER_OPS:
            if op in ("hue_wheel", "siemens_star"):
                cx, cy = p["centre_mm"]
                r = p["radius_mm"] + 1.0 / k
                rect = (cx - r, cy - r, cx + r, cy + r)
            else:
                rect = tuple(p["rect_mm"])
            px0, py0 = max(0, int(math.floor(rect[0] * k + 1e-6))), max(0, int(math.floor(rect[1] * k + 1e-6)))
            px1, py1 = min(W, int(math.ceil(rect[2] * k - 1e-6))), min(H, int(math.ceil(rect[3] * k - 1e-6)))
            if px1 <= px0 or py1 <= py0:
                continue
            crop = img.crop((px0, py0, px1, py1)).convert("L" if mono else "RGB")
            out.append({"op": "image", "x": px0 / k, "y": py0 / k, "w": (px1 - px0) / k, "h": (py1 - py0) / k, "px_w": px1 - px0,
                        "px_h": py1 - py0, "mode": "gray" if mono else "rgb", "data_b64": base64.b64encode(crop.tobytes()).decode("ascii"),
                        "id": eid})
        else:
            raise ValueError(f"unknown fakescan primitive op {op!r} (id {eid!r})")
    return out


@lru_cache(maxsize=24)
def _layout_v3(params: PageParams) -> dict[str, Any]:
    from fakescan import diag

    w, h = params.size_mm()
    dpi = layout_dpi(w, h)
    kw = v3_kwargs(params)
    lay = dict(diag.diag_page_layout(dpi, **kw))
    img = diag.render_diag_page(dpi, **kw)
    prims = _v3_pdf_primitives(lay, img, params.colour == "mono")
    by_id: dict[str, list[dict[str, Any]]] = {}
    for q in prims:
        by_id.setdefault(q["id"], []).append(q)
    ids = {e["id"] for e in lay["elements"]}
    els = []
    for e in lay["elements"]:
        e = dict(e)
        e["primitives"] = by_id.get(e["id"], []) + by_id.get(e["id"] + "_outline", [])
        els.append(e)
    extra = [q for i, v in by_id.items() for q in v if i not in ids and not (i.endswith("_outline") and i[:-8] in ids)]
    if extra:
        els.append({"id": "_extra", "kind": "extra", "group": "extra", "drawn": True, "rect_mm": [0, 0, 0, 0], "primitives": extra})
    lay["elements"] = els
    lay["pdf_primitives"] = prims  # paint order across elements; the writer draws this list
    lay["source"] = "fakescan.diag v3"
    lay["sheet_mm"] = [w, h]
    lay["layout_dpi"] = dpi
    return lay


def _layout_v2_fallback(params: PageParams, why: str) -> dict[str, Any]:
    """FALLBACK (clearly marked, only for an extension without layout v3): the fixed-size layout v2, embedded as one raster at the sheet's
    top-left corner at its physical size.  Portrait only; clipped on sheets smaller than 209 x 291 mm (``layout["fallback"]["clipped_mm"]``);
    no orientation glyphs, page / copy stamp or duplex set."""
    lay = dict(v2_layout(params.dpi if 50 <= params.dpi <= 300 else 150))
    w, h = params.size_mm()
    rw, rh = lay["raster_mm"]
    lay["source"] = "fakescan.diag v2 fallback"
    lay["sheet_mm"] = [w, h]
    lay["fallback"] = {
        "reason": why,
        "placement_mm": [0.0, 0.0],
        "raster_mm": [rw, rh],
        "clipped_mm": [max(0.0, rw - w), max(0.0, rh - h)],
        "orientation_supported": params.orientation == "portrait",
    }
    if params.orientation != "portrait":
        raise LayoutUnavailable("the v2 fallback layout is portrait only; landscape needs fakescan layout v3 (rebuild the fakescan extension)")
    return lay


def layout_for(params: PageParams) -> dict[str, Any]:
    """The layout JSON for ``params``: THE adapter, the only place that knows the layout source.

    Source: fakescan's parametric layout v3 (``fakescan.diag.diag_page_layout`` + ``diag_page_primitives``), the same page at any paper
    size / orientation.  The result is fakescan's layout dict (``elements`` with ground truth, ``qr_payload``, ``spec`` ...) plus

    * ``source`` = ``"fakescan.diag v3"``, ``sheet_mm``, ``layout_dpi`` (the density of the render the text cells / raster-only elements
      were read from);
    * ``pdf_primitives``: the whole page in paint order in the writer's primitive schema (see the module docstring), and the same
      primitives per element under ``elements[i]["primitives"]``.

    When the installed extension has no layout v3 the clearly marked v2 fallback is used (``source == "fakescan.diag v2 fallback"``,
    see :func:`_layout_v2_fallback`)."""
    try:
        from fakescan import diag  # noqa: F401
    except Exception as e:  # noqa: BLE001
        raise LayoutUnavailable(f"fakescan is not importable ({e}); the test page layout comes from fakescan.diag") from e
    if v3_available():
        return dict(_layout_v3(params))
    return _layout_v2_fallback(params, "the installed fakescan has no parametric layout v3 (diag_page_layout(dpi, **params))")


# ------------------------------------------------------------------------------------------------ the PDF writer

_FONTS = {
    ("sans", False): "Helvetica", ("sans", True): "Helvetica-Bold",
    ("serif", False): "Times-Roman", ("serif", True): "Times-Bold",
    ("mono", False): "Courier", ("mono", True): "Courier-Bold",
}


def _num(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _pdf_str(s: str) -> bytes:
    b = s.encode("cp1252", "replace")
    return b"(" + b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


class PdfWriter:
    """A one-sheet-per-page PDF writer for the primitive schema above.  Deterministic: no dates, fixed ``/ID``, Flate level 6."""

    def __init__(self, width_mm: float, height_mm: float, *, mono: bool = False, title: str = "fakeprint test page", subject: str = "") -> None:
        self.w_mm, self.h_mm, self.mono = width_mm, height_mm, mono
        self.title, self.subject = title, subject
        self.pages: list[tuple[bytes, dict[str, int]]] = []  # (content, {image name: object index in self.images})
        self.images: list[tuple[bytes, dict[str, Any]]] = []
        self._img_cache: dict[tuple, int] = {}
        self._fonts_used: set[str] = set()
        self.new_page()

    # -- page management
    def new_page(self) -> None:
        if self.pages:
            self._flush()
        self.pages.append((b"", {}))
        self._ops: list[str] = []
        self._xobj: dict[str, int] = {}

    def _flush(self) -> None:
        self.pages[-1] = ("\n".join(self._ops).encode("latin-1"), dict(self._xobj))

    # -- colour / geometry helpers
    def _col(self, rgb: list[int] | tuple[int, ...], stroke: bool) -> str:
        r, g, b = (max(0, min(255, int(c))) / 255.0 for c in rgb)
        if self.mono:
            y = 0.299 * r + 0.587 * g + 0.114 * b
            return f"{_num(y)} {'G' if stroke else 'g'}"
        return f"{_num(r)} {_num(g)} {_num(b)} {'RG' if stroke else 'rg'}"

    def _x(self, mm: float) -> str:
        return _num(mm * PT / MM)

    def _y(self, mm: float) -> str:
        return _num((self.h_mm - mm) * PT / MM)

    # -- primitives
    def draw(self, p: dict[str, Any]) -> None:
        op = p["op"]
        o = self._ops
        if op == "line":
            o.append("q " + self._col(p.get("rgb", [0, 0, 0]), True) + f" {_num(p.get('w', 0.2) * PT / MM)} w "
                     + (f"[{_num(p['dash'][0] * PT / MM)} {_num(p['dash'][1] * PT / MM)}] 0 d " if p.get("dash") else "[] 0 d ")
                     + f"{self._x(p['x1'])} {self._y(p['y1'])} m {self._x(p['x2'])} {self._y(p['y2'])} l S Q")
        elif op == "rect":
            fill, stroke = p.get("fill"), p.get("stroke")
            if fill is None and stroke is None:
                fill = [0, 0, 0]
            o.append("q " + self._paint_path_prefix(fill, stroke, p)
                     + f"{self._x(p['x'])} {self._y(p['y'] + p['h'])} {_num(p['w'] * PT / MM)} {_num(p['h'] * PT / MM)} re "
                     + self._paint_op(fill, stroke) + " Q")
        elif op == "rects":
            if p["rects"]:
                o.append("q " + self._col(p.get("fill", [0, 0, 0]), False) + " "
                         + " ".join(f"{self._x(x)} {self._y(y + h)} {_num(w * PT / MM)} {_num(h * PT / MM)} re" for x, y, w, h in p["rects"]) + " f Q")
        elif op == "poly":
            pts = p["pts"]
            fill, stroke = p.get("fill"), p.get("stroke")
            if fill is None and stroke is None:
                fill = [0, 0, 0]
            path = f"{self._x(pts[0][0])} {self._y(pts[0][1])} m " + " ".join(f"{self._x(x)} {self._y(y)} l" for x, y in pts[1:])
            if p.get("close", True):
                path += " h"
            o.append("q " + self._paint_path_prefix(fill, stroke, p) + path + " " + self._paint_op(fill, stroke) + " Q")
        elif op == "circle":
            cx, cy, r = p["cx"], p["cy"], p["r"]
            k = 0.5522847498 * r
            pts = [(cx + r, cy), (cx + r, cy + k, cx + k, cy + r, cx, cy + r), (cx - k, cy + r, cx - r, cy + k, cx - r, cy),
                   (cx - r, cy - k, cx - k, cy - r, cx, cy - r), (cx + k, cy - r, cx + r, cy - k, cx + r, cy)]
            path = f"{self._x(pts[0][0])} {self._y(pts[0][1])} m " + " ".join(
                " ".join(f"{self._x(c[i])} {self._y(c[i + 1])}" for i in range(0, 6, 2)) + " c" for c in pts[1:]) + " h"
            fill, stroke = p.get("fill"), p.get("stroke")
            if fill is None and stroke is None:
                fill = [0, 0, 0]
            o.append("q " + self._paint_path_prefix(fill, stroke, p) + path + " " + self._paint_op(fill, stroke) + " Q")
        elif op == "text":
            font = _FONTS[(p.get("font", "sans"), bool(p.get("bold")))]
            self._fonts_used.add(font)
            size = p.get("size_pt", 10)
            text = str(p["text"])
            x, y = p["x"], p["y"]
            anchor = p.get("anchor", "start")
            ang = math.radians(p.get("rotate_deg", 0.0))
            c, s = math.cos(ang), math.sin(ang)
            adv = _text_width_pt(font, text, size) * (0.5 if anchor == "middle" else 1.0 if anchor == "end" else 0.0)
            # text matrix: rotation about the anchor point; the shift back along the baseline is applied in text space
            o.append(f"BT /{font} {_num(size)} Tf {self._col(p.get('rgb', [0, 0, 0]), False)} "
                     f"{_num(c)} {_num(s)} {_num(-s)} {_num(c)} {self._x(x)} {self._y(y)} Tm {_num(-adv)} 0 Td ")
            o[-1] += _pdf_str(text).decode("latin-1") + " Tj ET"
        elif op == "image":
            raw = base64.b64decode(p["data_b64"])
            mode = p.get("mode", "gray")
            if self.mono and mode == "rgb":
                raw = bytes(int(0.299 * raw[i] + 0.587 * raw[i + 1] + 0.114 * raw[i + 2] + 0.5) for i in range(0, len(raw), 3))
                mode = "gray"
            key = (hashlib.md5(raw).hexdigest(), p["px_w"], p["px_h"], mode)
            idx = self._img_cache.get(key, -1)
            if idx < 0:
                idx = self._img_cache[key] = len(self.images)
                self.images.append((zlib.compress(raw, 6), {"w": p["px_w"], "h": p["px_h"], "cs": "DeviceGray" if mode == "gray" else "DeviceRGB"}))
            name = f"Im{idx}"
            self._xobj[name] = idx
            o.append(f"q {_num(p['w'] * PT / MM)} 0 0 {_num(p['h'] * PT / MM)} {self._x(p['x'])} {self._y(p['y'] + p['h'])} cm /{name} Do Q")
        elif op == "qr":
            mods = p["modules"]
            n = len(mods)
            quiet = p.get("quiet", 0)
            pitch = p["size"] / (n + 2 * quiet)
            o.append("q " + self._col([0, 0, 0], False))
            for r, row in enumerate(mods):
                c = 0
                while c < n:
                    if row[c]:
                        c0 = c
                        while c < n and row[c]:
                            c += 1
                        o.append(f"{self._x(p['x'] + (quiet + c0) * pitch)} {self._y(p['y'] + (quiet + r + 1) * pitch)} "
                                 f"{_num((c - c0) * pitch * PT / MM)} {_num(pitch * PT / MM)} re")
                    else:
                        c += 1
            o.append("f Q")
        else:
            raise ValueError(f"unknown primitive op {op!r}")

    def _paint_path_prefix(self, fill: Any, stroke: Any, p: dict[str, Any]) -> str:
        out = []
        if fill is not None:
            out.append(self._col(fill, False))
        if stroke is not None:
            out.append(self._col(stroke, True))
            out.append(f"{_num(p.get('sw', 0.2) * PT / MM)} w")
            d = p.get("dash")
            out.append(f"[{_num(d[0] * PT / MM)} {_num(d[1] * PT / MM)}] 0 d" if d else "[] 0 d")
        return " ".join(out) + (" " if out else "")

    @staticmethod
    def _paint_op(fill: Any, stroke: Any) -> str:
        return "B" if fill is not None and stroke is not None else "f" if fill is not None else "S"

    # -- output
    def to_bytes(self) -> bytes:
        self._flush()
        W, H = self.w_mm * PT / MM, self.h_mm * PT / MM
        objs: list[bytes] = []  # object n is objs[n-1]

        def add(b: bytes) -> int:
            objs.append(b)
            return len(objs)

        cat = add(b"")  # 1: catalog (patched)
        pages_obj = add(b"")  # 2: pages (patched)
        font_ids = {}
        for fname in sorted(_FONTS.values()):
            font_ids[fname] = add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{fname} /Encoding /WinAnsiEncoding >>".encode())
        img_ids = []
        for data, meta in self.images:
            img_ids.append(add(
                f"<< /Type /XObject /Subtype /Image /Width {meta['w']} /Height {meta['h']} /ColorSpace /{meta['cs']} "
                f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"))
        kids = []
        fonts_res = " ".join(f"/{n} {i} 0 R" for n, i in font_ids.items())
        info = add(b"<< /Title " + _pdf_str(self.title) + b" /Subject " + _pdf_str(self.subject) + b" /Producer (fakeprint.testpage) >>")
        for content, xobj in self.pages:
            comp = zlib.compress(content, 6)
            cid = add(f"<< /Length {len(comp)} /Filter /FlateDecode >>\nstream\n".encode() + comp + b"\nendstream")
            xres = " ".join(f"/{n} {img_ids[i]} 0 R" for n, i in xobj.items())
            kids.append(add(
                f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {_num(W)} {_num(H)}] /Contents {cid} 0 R "
                f"/Resources << /Font << {fonts_res} >> /XObject << {xres} >> /ProcSet [/PDF /Text /ImageB /ImageC] >> >>".encode()))
        objs[pages_obj - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
        objs[cat - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offs = []
        for i, o in enumerate(objs, 1):
            offs.append(len(out))
            out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
        x = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        for o in offs:
            out += b"%010d 00000 n \n" % o
        docid = hashlib.md5(bytes(out)).hexdigest().upper().encode()
        out += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R /ID [<" % (len(objs) + 1, cat, info)) + docid + b"><" + docid + b">] >>\n"
        out += b"startxref\n%d\n%%%%EOF\n" % x
        return bytes(out)


# Helvetica / Times / Courier average advance widths (per em) good enough for anchoring text.
def _text_width_pt(font: str, text: str, size: float) -> float:
    if font.startswith("Courier"):
        return 0.6 * size * len(text)
    avg = 0.55 if font.startswith("Helvetica") else 0.5
    if font.endswith("Bold"):
        avg += 0.04
    return avg * size * len(text)


def _fallback_draw(pw: PdfWriter, params: PageParams, lay: dict[str, Any]) -> None:
    """The v2 fallback: the fakescan rendering placed at its physical size at the sheet's top-left corner."""
    dpi = int(round(lay["dpi_effective"]))
    img = v2_render(dpi).convert("L" if params.colour == "mono" else "RGB")
    rw, rh = lay["raster_mm"]
    pw.draw({"op": "image", "x": 0.0, "y": 0.0, "w": rw, "h": rh, "px_w": img.width, "px_h": img.height,
             "mode": "gray" if params.colour == "mono" else "rgb", "data_b64": base64.b64encode(img.tobytes()).decode("ascii")})


@dataclass
class TestPage:
    __test__ = False  # not a pytest class

    params: PageParams
    layout: dict[str, Any]
    pdf: bytes
    meta: dict[str, Any] = field(default_factory=dict)

    def png(self, dpi: int | None = None) -> bytes:
        return render_png(self.pdf, dpi or self.params.dpi)


def coalesce_rects(prims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge every run of consecutive plain filled ``rect`` ops of one colour (no stroke, no dash) into one ``rects`` op.

    fakescan's revision-1 fiducials (ArUco modules, chessboard / ChArUco squares, brackets, vertical ruler ticks, 5x7 text cells) are
    rectangles that TOUCH.  Painted one by one, a rasteriser anti-aliases each shared edge twice (coverage 0.5 x 0.5 composites to 75 %
    ink, not 100 %): a faint seam that moves marker corners and breaks a chessboard at some zoom levels.  One path with non-zero winding
    is rasterised as a union, so a seam cannot appear at any zoom.  Paint order is kept (only adjacent ops of one colour merge)."""
    out: list[dict[str, Any]] = []
    for p in prims:
        if p.get("op") == "rect" and p.get("stroke") is None and not p.get("dash") and p.get("fill") is not None:
            fill = list(p["fill"])
            if out and out[-1].get("op") == "rects" and out[-1]["fill"] == fill:
                out[-1]["rects"].append([p["x"], p["y"], p["w"], p["h"]])
            else:
                out.append({"op": "rects", "rects": [[p["x"], p["y"], p["w"], p["h"]]], "fill": fill})
        else:
            out.append(p)
    return out


def _draw_page(pw: PdfWriter, params: PageParams, lay: dict[str, Any]) -> str:
    prims = lay.get("pdf_primitives") or [p for e in lay["elements"] if e.get("drawn", True) for p in e.get("primitives", [])]
    if not prims:
        _fallback_draw(pw, params, lay)
        return "fallback-raster"
    for p in coalesce_rects(prims):
        pw.draw(p)
    return "vector"


def _subject(params: PageParams, sequence: list[list[Any]] | None = None) -> str:
    d: dict[str, Any] = params.to_dict()
    if sequence:
        d["sequence"] = sequence
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


def render_pdf(params: PageParams, layout: dict[str, Any] | None = None) -> TestPage:
    """The test page for ``params`` as PDF bytes (deterministic: the same parameters give the same bytes)."""
    lay = layout if layout is not None else layout_for(params)
    w, h = params.size_mm()
    pw = PdfWriter(w, h, mono=params.colour == "mono", subject=_subject(params))
    mode = _draw_page(pw, params, lay)
    return TestPage(params, lay, pw.to_bytes(), {"mode": mode})


def document_sequence(params: PageParams, *, expand_copies: bool = False, collate: bool = True, duplex: bool = False) -> list[PageParams]:
    """The per-sheet-side parameters of a multi-page test document, in document order.  ``params.pages`` pages; with ``expand_copies``
    the document carries ``params.copies`` physical copies itself (``collate``: p1 p2 p1 p2, else p1 p1 p2 p2), otherwise it carries one
    copy stamped ``COPY 1 OF C`` and the PRINTER makes the others.  ``duplex`` alternates front / back by position in the document
    (sides 1,3,5.. are fronts)."""
    n, c = params.pages, params.copies
    order = [(pg, cp) for cp in range(1, c + 1) for pg in range(1, n + 1)] if expand_copies and collate else             [(pg, cp) for pg in range(1, n + 1) for cp in range(1, c + 1)] if expand_copies else             [(pg, 1) for pg in range(1, n + 1)]
    return [params.replace(page=pg, copy=cp, side=("front" if i % 2 == 0 else "back") if duplex else "front") for i, (pg, cp) in enumerate(order)]


def render_document(params: PageParams, *, expand_copies: bool = False, collate: bool = True, duplex: bool = False,
                    layout_source: Any = None) -> TestPage:
    """A multi-page test document (see :func:`document_sequence`) as one PDF; each page is the layout for its own page / copy / side.
    ``layout_source`` (default :func:`layout_for`) maps a :class:`PageParams` to a layout.  The PDF /Subject records the base
    parameters and the ``sequence`` of ``[page, copy, side]`` so the analyzer can expect the right stamps per page."""
    seq = document_sequence(params, expand_copies=expand_copies, collate=collate, duplex=duplex)
    src = layout_source or layout_for
    w, h = params.size_mm()
    pw = PdfWriter(w, h, mono=params.colour == "mono", subject=_subject(params, [[q.page, q.copy, q.side] for q in seq]))
    modes = set()
    lay0 = None
    for i, q in enumerate(seq):
        if i:
            pw.new_page()
        lay = src(q)
        lay0 = lay0 or lay
        modes.add(_draw_page(pw, q, lay))
    return TestPage(params, lay0 or {}, pw.to_bytes(), {"mode": "+".join(sorted(modes)), "sequence": [[q.page, q.copy, q.side] for q in seq]})


def render_png(pdf: bytes, dpi: int = 150) -> bytes:
    """Page 1 of ``pdf`` as a PNG at exactly ``dpi`` (needs pypdfium2 and Pillow); the PNG carries the density."""
    from .testpage_analyze import render_pdf_exact

    r = render_pdf_exact(pdf, dpi)
    buf = io.BytesIO()
    r.img.save(buf, "PNG", dpi=(dpi, dpi))
    return buf.getvalue()
