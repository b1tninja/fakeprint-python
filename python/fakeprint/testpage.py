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


def layout_for(params: PageParams) -> dict[str, Any]:
    """The layout JSON for ``params``: THE adapter, the only place that knows the layout source.

    EXPECTED (fakescan side, agent A; not landed when this was written)::

        fakescan.diag.page_layout(params: dict) -> dict      # params = PageParams.to_dict()

    returning ``{"schema", "layout_version", "sheet_mm": [w, h], "elements": [...], "qr_payload": str}`` with each element
    carrying ``primitives`` in the schema of this module's docstring (sheet-relative mm, y down) plus ``expected`` ground truth, and
    elements that do not fit the sheet ``drawn: false`` (or ``scale``).  When that function exists it is used as is.

    FALLBACK (clearly marked, NOT the target design): the installed ``fakescan.diag`` layout v2 (``diag_page_layout``).  It is a
    fixed 209.3 x 290.6 mm raster with no primitives, so the writer embeds its rendering at the sheet's top-left corner at its
    own physical size.  It is only meaningful on sheets about A4 size and larger; on smaller sheets (Letter is 279.4 mm tall) the
    page is CLIPPED by the sheet, which is recorded in ``layout["fallback"]["clipped_mm"]``.  It has no corner orientation
    markers, page / copy stamp or duplex set: those need fakescan's parametric layout.  ``layout["source"]`` says which path ran.
    """
    try:
        from fakescan import diag
    except Exception as e:  # noqa: BLE001
        raise LayoutUnavailable(f"fakescan is not importable ({e}); the test page layout comes from fakescan.diag") from e
    fn = getattr(diag, "page_layout", None)
    if fn is not None:
        lay = dict(fn(params.to_dict()))
        lay.setdefault("source", "fakescan.diag.page_layout")
        return lay
    lay = dict(diag.diag_page_layout(params.dpi if 50 <= params.dpi <= 300 else 150))
    w, h = params.size_mm()
    rw, rh = lay["raster_mm"]
    lay["source"] = "fakescan.diag v2 fallback"
    lay["sheet_mm"] = [w, h]
    lay["fallback"] = {
        "reason": "fakescan.diag.page_layout (parametric layout) is not available",
        "placement_mm": [0.0, 0.0],
        "raster_mm": [rw, rh],
        "clipped_mm": [max(0.0, rw - w), max(0.0, rh - h)],
        "orientation_supported": params.orientation == "portrait",
    }
    if params.orientation != "portrait":
        raise LayoutUnavailable("the v2 fallback layout is portrait only; landscape needs fakescan.diag.page_layout")
    return lay


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
    from fakescan import diag

    dpi = int(round(lay["dpi_effective"]))
    img = diag.render_diag_page(dpi).convert("L" if params.colour == "mono" else "RGB")
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


def _draw_page(pw: PdfWriter, params: PageParams, lay: dict[str, Any]) -> str:
    prims = [p for e in lay["elements"] if e.get("drawn", True) for p in e.get("primitives", [])]
    if not prims:
        _fallback_draw(pw, params, lay)
        return "fallback-raster"
    for p in prims:
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
