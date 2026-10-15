"""The print test page (``fakeprint.testpage``) and the received-document analyzer (``fakeprint.testpage_analyze``).

No page fixtures: every page here is generated from parameters.  The writer is tested with a small layout made IN THIS FILE
(a few primitives at known millimetre positions) at eight sheet shapes; the adapter and analyzer are tested on fakescan's layout v3 at
A4, Letter, A5, A3, Legal, a custom size and landscape: the layout's own samples are checked against the pixels of the PDF rasterised
through pdfium, and orientation faults (rotations, mirrors) injected into the raster are detected.
"""

from __future__ import annotations

import base64
import json
import zlib

import pytest

pytest.importorskip("PIL")
pytest.importorskip("pypdfium2")

from fakeprint import qrdecode, testpage as tp, testpage_analyze as ta  # noqa: E402

MM = 25.4


def _double_layout(w: float, h: float) -> dict:
    """Elements at fixed offsets from the sheet corners, so the same assertions hold at any size."""
    gray = bytes([0, 64, 128, 192, 255, 255])
    return {"source": "test double", "sheet_mm": [w, h], "elements": [
        {"id": "tl", "kind": "swatch", "drawn": True, "primitives": [{"op": "rect", "x": 5, "y": 5, "w": 10, "h": 10, "fill": [255, 0, 0]}]},
        {"id": "br", "kind": "swatch", "drawn": True, "primitives": [{"op": "rect", "x": w - 15, "y": h - 15, "w": 10, "h": 10, "fill": [0, 0, 255]}]},
        {"id": "disc", "kind": "swatch", "drawn": True, "primitives": [{"op": "circle", "cx": w / 2, "cy": h / 2, "r": 8, "fill": [0, 160, 0]}]},
        {"id": "hair", "kind": "frame", "drawn": True, "primitives": [{"op": "line", "x1": 3, "y1": h / 2, "x2": w - 3, "y2": h / 2, "w": 0.5}]},
        {"id": "img", "kind": "ramp", "drawn": True, "primitives": [{"op": "image", "x": 20, "y": 20, "w": 12, "h": 4, "px_w": 6, "px_h": 1, "mode": "gray",
                                                                   "data_b64": base64.b64encode(gray).decode()}]},
        {"id": "txt", "kind": "text", "drawn": True, "primitives": [{"op": "text", "x": 20, "y": 40, "size_pt": 12, "text": "Hello (test) \\ ä€", "bold": True}]},
        {"id": "qr", "kind": "qr", "drawn": True, "primitives": [{"op": "qr", "x": 40, "y": 60, "size": 10, "modules": [[1, 0], [0, 1]]}]},
        {"id": "gone", "kind": "swatch", "drawn": False, "scale": 0.0, "primitives": [{"op": "rect", "x": 100, "y": 100, "w": 5, "h": 5, "fill": [0, 0, 0]}]},
    ]}


def _px(r: ta.Raster, x_mm: float, y_mm: float):
    return r.img.getpixel((int(x_mm / MM * r.dpi_x), int(y_mm / MM * r.dpi_y)))


# ---- paper registry ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name,size", [("A4", (210, 297)), ("a3", (297, 420)), ("A5", (148, 210)), ("Letter", (215.9, 279.4)), ("Legal", (215.9, 355.6)),
                                       ("Tabloid", (279.4, 431.8)), ("ISO B5", (176, 250)), ("JIS-B4", (257, 364)), ("b4", (250, 353)),
                                       ("210x297", (210, 297)), ("100 x 150 mm", (100, 150))])
def test_paper_size(name, size):
    assert tp.paper_size(name) == pytest.approx(size)


def test_paper_size_rejects():
    for bad in ("a99", "", "0x10", "foo"):
        with pytest.raises(ValueError):
            tp.paper_size(bad)


def test_params_orientation_custom_and_validation():
    assert tp.PageParams(paper="a4", orientation="landscape").size_mm() == (297.0, 210.0)
    assert tp.PageParams(width_mm=420, height_mm=123).size_mm() == (123.0, 420.0)
    assert tp.PageParams.from_dict(tp.PageParams(paper="letter", copies=3, copy=2, pages=2, page=2, side="back").to_dict()).side == "back"
    for kw in ({"orientation": "sideways"}, {"colour": "pink"}, {"page": 3, "pages": 2}, {"job": "has space"}, {"job": "x" * 17}, {"side": "left"}):
        with pytest.raises(ValueError):
            tp.PageParams(**kw)


# ---- the writer at any size ----------------------------------------------------------------------------

SHEETS = [("a4", "portrait"), ("letter", "portrait"), ("a5", "portrait"), ("a3", "portrait"), ("legal", "portrait"), ("123x456", "portrait"),
          ("a4", "landscape"), ("letter", "landscape")]


@pytest.mark.parametrize("paper,orient", SHEETS)
def test_writer_places_primitives_by_mm_at_any_size(paper, orient):
    p = tp.PageParams(paper=paper, orientation=orient)
    w, h = p.size_mm()
    page = tp.render_pdf(p, _double_layout(w, h))
    assert page.meta["mode"] == "vector"
    facts = ta.pdf_facts(page.pdf)
    assert facts["pages"] == 1 and facts["page_boxes"][0]["size_mm"] == pytest.approx([w, h], abs=0.01)
    assert facts["page_boxes"][0]["effective_box"] == "MediaBox"
    r = ta.render_pdf_exact(page.pdf, 50)
    assert _px(r, 10, 10) == (255, 0, 0)  # top-left red square centre
    assert _px(r, w - 10, h - 10) == (0, 0, 255)  # bottom-right blue square centre (y is DOWN: not mirrored)
    assert _px(r, w / 2, h / 2 - 3) == (0, 160, 0)  # disc
    assert _px(r, 100.0 if w > 110 else w / 2, 102.5)[0] > 240 or w <= 110  # the undrawn element is absent
    assert _px(r, 20.6, 22)[0] < 40 and _px(r, 31.4, 22)[0] > 240  # image: first sample black, last white (nearest, not stretched wrongly)
    assert _px(r, 42, 62) == (0, 0, 0) and _px(r, 47, 62)[0] > 240  # qr module (0,0) dark, (0,1) light
    r2 = ta.render_pdf_exact(page.pdf, 100)
    col = int(60 / MM * 100)
    assert min(r2.img.getpixel((col, y))[0] for y in range(int(h / 2 / MM * 100) - 2, int(h / 2 / MM * 100) + 3)) < 100  # the 0.5 mm line


def test_writer_is_deterministic_and_mono_is_gray():
    p = tp.PageParams(paper="a5")
    lay = _double_layout(*p.size_mm())
    a, b = tp.render_pdf(p, lay).pdf, tp.render_pdf(p, lay).pdf
    assert a == b and a.startswith(b"%PDF-1.4")
    m = tp.render_pdf(p.replace(colour="mono"), lay).pdf
    assert m != a
    r = ta.render_pdf_exact(m, 50)
    px = _px(r, 10, 10)
    assert px[0] == px[1] == px[2] and 70 <= px[0] <= 80  # luma of pure red
    assert ta.params_from_pdf(a) == p


def test_writer_unknown_op_is_an_error():
    w = tp.PdfWriter(100, 100)
    with pytest.raises(ValueError):
        w.draw({"op": "spline"})


def test_png_has_stated_dpi_and_size():
    from PIL import Image
    import io

    p = tp.PageParams(paper="a5", dpi=100)
    page = tp.render_pdf(p, _double_layout(*p.size_mm()))
    im = Image.open(io.BytesIO(page.png()))
    assert im.size == (int(148 / MM * 100), int(210 / MM * 100)) and round(im.info["dpi"][0]) == 100
    im2 = Image.open(io.BytesIO(page.png(200)))
    assert round(im2.info["dpi"][0]) == 200 and im2.width > im.width * 1.9


# ---- the adapter ------------------------------------------------------------------------------------------


def test_adapter_is_layout_v3_and_the_v2_fallback_is_marked(monkeypatch):
    pytest.importorskip("fakescan.diag")
    if not tp.v3_available():
        pytest.skip("the installed fakescan extension has no layout v3 (rebuild fakescan-python)")
    lay = tp.layout_for(tp.PageParams(paper="a4"))
    assert lay["source"] == "fakescan.diag v3" and lay["layout_version"] == 3 and "fallback" not in lay
    assert lay["sheet_mm"] == [210.0, 297.0] and lay["spec"]["paper"] == "A4" and lay["pdf_primitives"]
    # the v2 fallback (an extension without v3) says so, is portrait only and records what the sheet clips
    monkeypatch.setattr(tp, "v3_available", lambda: False)
    fb = tp.layout_for(tp.PageParams(paper="a4"))
    assert fb["source"] == "fakescan.diag v2 fallback" and fb["fallback"]["clipped_mm"] == [0.0, 0.0]
    letter = tp.layout_for(tp.PageParams(paper="letter"))
    assert letter["fallback"]["clipped_mm"][1] == pytest.approx(290.576 - 279.4, abs=0.01)  # clipped by the sheet, recorded
    with pytest.raises(tp.LayoutUnavailable):
        tp.layout_for(tp.PageParams(paper="a4", orientation="landscape"))
    assert tp.render_pdf(tp.PageParams(paper="a5")).meta["mode"] == "fallback-raster"


def test_v3_parameter_mapping():
    pytest.importorskip("fakescan.diag")
    k = tp.v3_kwargs(tp.PageParams(paper="isob5", page=2, pages=4, copy=3, copies=3, side="back", job="a.b", colour="mono", margin_mm=5))
    assert k["paper"] == "B5" and k["page"] == 1 and k["total"] == 4 and k["copy"] == 3 and k["side"] == "back"
    assert k["job"] == "A-B" and k["colour"] == "gray" and k["margin_mm"] == 5 and "width_mm" not in k
    k = tp.v3_kwargs(tp.PageParams(width_mm=420, height_mm=123, orientation="landscape"))
    assert k["width_mm"] == 123 and k["height_mm"] == 420 and k["orientation"] == "landscape" and "paper" not in k


# ---- the QR decoder ------------------------------------------------------------------------------------------


def test_qr_decoder_reads_fakescans_qr():
    pytest.importorskip("fakescan.diag")
    lay = tp.v2_layout(150)
    im = tp.v2_render(150).convert("L")
    px = im.load()
    q = next(e for e in lay["elements"] if e["kind"] == "qr")
    k = lay["px_per_mm"]
    x0, y0, x1, y1 = (v * k for v in q["rect_mm"])
    res = qrdecode.decode_region(lambda x, y: px[min(max(int(x), 0), im.width - 1), min(max(int(y), 0), im.height - 1)], (x0, y0, x1, y1))
    assert res.ok and res.ec_ok and res.version == 4 and res.ecl == "M"
    assert res.text.startswith("fakescan|v1|") and "lay=2" in res.text


def test_qr_decoder_tables_are_consistent():
    for (v, lvl), (ec, groups) in qrdecode._EC.items():
        total = sum(n * (d + ec) for n, d in groups)
        fn = qrdecode._function_mask(v)
        bits = sum(1 for r in fn for c in r if not c)
        assert bits // 8 == total, (v, lvl)


def test_qr_payload_fields():
    f = ta.parse_qr_payload("fakeprint|job=abc|page=2|pages=3|side=back")
    assert f["job"] == "abc" and f["page"] == "2"
    enc = base64.urlsafe_b64encode(json.dumps({"page": 2}).encode()).decode().rstrip("=")
    assert ta.parse_qr_payload(f"x|params={enc}")["params"] == {"page": 2}


# ---- the analyzer ---------------------------------------------------------------------------------------------

fakescan_diag = pytest.importorskip("fakescan.diag")


@pytest.fixture(scope="module")
def a4():
    p = tp.PageParams(paper="a4", job="t1")
    page = tp.render_pdf(p)
    return p, page, ta.render_pdf_exact(page.pdf, 150)


def _variant(r: ta.Raster, img) -> ta.Raster:
    swap = img.width != r.img.width
    return ta.Raster(img, r.dpi_y if swap else r.dpi_x, r.dpi_x if swap else r.dpi_y, None, "test", [])


def test_analyzer_upright_a4(a4):
    p, page, r = a4
    rep = ta.analyze_document(page.pdf, params=p)
    s = rep["summary"]
    assert rep["is_test_page"] and s["verdict_orientation"] == "upright" and not s["mirrored"]
    assert s["scale_x"] == pytest.approx(1.0, abs=0.002) and s["scale_y"] == pytest.approx(1.0, abs=0.002)
    assert s["clipped_edges"] == [] and s["sheet_size_ok"] and s["sheet_mm"] == pytest.approx([210, 297], abs=0.3)
    assert s["qr_ok"] and "lay=3" in s["qr_text"] and "paper=A4" in s["qr_text"]
    assert rep["document"]["page_boxes"][0]["effective_box"] == "MediaBox" and rep["document"]["images"]  # ramps / wheel / star / zone plate / QR
    assert "elements" in rep and "res_lines_v" in rep["elements"] and rep["elements"]["colorchecker24"]["mean_delta_e76"] < 1.5
    assert s["page"] == 1 and s["side"] == "front" and s["glyph_orientation"] == "upright" and s["page_order_ok"]
    # parameters recovered from the PDF itself
    assert ta.analyze_document(page.pdf)["summary"]["scale_x"] == pytest.approx(1.0, abs=0.002)


@pytest.mark.parametrize("op,verdict,mirrored", [
    ("ROTATE_180", "rotated 180 degrees", False),
    ("ROTATE_90", "rotated 90 degrees counter-clockwise", False),  # the page was turned 90 degrees clockwise by ROTATE_270 on its way here
    ("ROTATE_270", "rotated 90 degrees clockwise", False),
    ("FLIP_LEFT_RIGHT", "mirrored left-right", True),
    ("FLIP_TOP_BOTTOM", "flipped top-bottom", True),
])
def test_analyzer_orientation_verdicts(a4, op, verdict, mirrored):
    from PIL import Image

    p, page, r = a4
    # `op` is what the PATH did to the page; the analyzer must name that defect
    received = _variant(r, r.img.transpose(getattr(Image.Transpose, op)))
    inv = {"ROTATE_90": "rotated 90 degrees clockwise"}
    rep = ta.analyze_raster(received, p, detail="fast")
    assert rep["is_test_page"]
    got = rep["summary"]["verdict_orientation"]
    expect = {"ROTATE_90": "rotated 90 degrees counter-clockwise"}.get(op, verdict)
    if op == "ROTATE_270":
        expect = "rotated 90 degrees clockwise"
    assert got == expect, got
    assert rep["summary"]["mirrored"] is mirrored
    assert rep["summary"]["scale_x"] == pytest.approx(1.0, abs=0.003) and rep["summary"]["scale_y"] == pytest.approx(1.0, abs=0.003)
    assert not rep["orientation"]["ambiguous"]


def test_analyzer_scale_and_shift(a4):
    from PIL import Image

    p, page, r = a4
    f = 0.94
    small = r.img.resize((round(r.img.width * f), round(r.img.height * f)), Image.Resampling.LANCZOS)
    rep = ta.analyze_raster(ta.Raster(small, 150, 150, None, "test"), p, detail="fast")
    s = rep["summary"]
    assert rep["is_test_page"] and s["scale_x"] == pytest.approx(f, abs=0.004) and s["scale_y"] == pytest.approx(f, abs=0.004)
    assert not s["sheet_size_ok"]  # the page came out smaller than the sheet it should be


def test_analyzer_clipping(a4):
    p, page, r = a4
    cut = r.img.crop((0, 0, r.img.width - 40, r.img.height - 60))  # lose 6.8 mm at the right and 10 mm at the bottom (crosses survive)
    rep = ta.analyze_raster(ta.Raster(cut, 150, 150, None, "test"), p, detail="fast")
    s = rep["summary"]
    assert rep["is_test_page"] and s["clipped_edges"]
    assert {"bottom", "right"} <= set(s["clipped_edges"])  # the printable frame (6.35 mm in) is gone at both cut edges, the left / top ones survive
    assert "left" not in s["clipped_edges"] and "top" not in s["clipped_edges"]
    assert s["scale_x"] == pytest.approx(1.0, abs=0.003)


def test_analyzer_png_roundtrip_and_density(a4):
    p, page, r = a4
    png = page.png(150)
    rep = ta.analyze_document(png, "image/png", p, detail="fast")
    assert rep["is_test_page"] and rep["summary"]["scale_x"] == pytest.approx(1.0, abs=0.003)
    assert rep["summary"]["sheet_mm"] == pytest.approx([210, 297], abs=0.3)


def test_analyzer_foreign_documents_degrade():
    w = tp.PdfWriter(210, 297)
    w.draw({"op": "text", "x": 20, "y": 30, "size_pt": 14, "text": "an invoice, not a test page"})
    w.draw({"op": "rect", "x": 20, "y": 50, "w": 100, "h": 3, "fill": [10, 10, 10]})
    doc = w.to_bytes()
    rep = ta.analyze_document(doc)
    assert not rep["is_test_page"] and "not a fakeprint test page" in rep["reason"] and rep["sheet_mm"] == pytest.approx([210, 297], abs=0.3)
    rep = ta.analyze_document(doc, params=tp.PageParams(paper="a4"))
    assert not rep["is_test_page"] and "not a fakeprint test page" in rep["reason"] and rep["document"]["pages"] == 1
    assert ta.brief(rep)["is_test_page"] is False
    junk = ta.analyze_document(b"%PDF-garbage")
    assert not junk["is_test_page"] and "unreadable" in junk["reason"]


def test_analyzer_multipage_lists_pages():
    p = tp.PageParams(paper="a4")
    one = tp.render_pdf(p).pdf
    # a second page appended by the writer: two pages of the same test page
    w = tp.PdfWriter(210, 297, subject=json.dumps(p.to_dict()))
    w.draw({"op": "rect", "x": 10, "y": 10, "w": 5, "h": 5})
    w.new_page()
    w.draw({"op": "rect", "x": 10, "y": 10, "w": 5, "h": 5})
    two = w.to_bytes()
    rep = ta.analyze_document(two, params=p, pages="all", detail="fast")
    assert rep["document_pages"] == 2 and len(rep["pages"]) == 2 and rep["document"]["pages"] == 2
    assert one  # the single page still analyses (above)


def test_analyzer_cut_corners_say_so(a4):
    p, page, r = a4
    cut = r.img.crop((0, 0, r.img.width - 400, r.img.height - 600))  # 68 mm right and 102 mm bottom: three of the four crosses are gone
    rep = ta.analyze_raster(ta.Raster(cut, 150, 150, None, "test"), p, detail="fast")
    assert rep["is_test_page"] and rep["summary"]["fit"] == "failed" and "registration" in rep["reason"]


@pytest.mark.embedded
def test_client_job_analysis_of_a_received_page(srv, pc):
    page = tp.render_pdf(tp.PageParams(paper="a4", job="jobtag"))
    jid = pc.print_document(page.pdf, format="application/pdf")
    c = srv.client()
    sm = c.job_analysis(jid)
    assert sm["job_id"] == jid and sm["summary"]["is_test_page"] and sm["summary"]["verdict_orientation"] == "upright"
    assert sm["summary"]["scale_x"] == pytest.approx(1.0, abs=0.003)
    job = c.job(jid, analysis=True)
    assert job.analysis["is_test_page"] and job.analysis["clipped_edges"] == []
    assert c.job(jid).analysis is None  # lazy: only on request
    jid2 = pc.print_document(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n", format="application/pdf")
    foreign = c.job_analysis(jid2)["summary"]
    assert foreign["is_test_page"] is False and "not a fakeprint test page" in foreign["reason"] or "unreadable" in foreign["reason"]


def test_document_sequences_collate_duplex_and_stamps():
    p = tp.PageParams(paper="a4", pages=2, copies=2)
    seq = lambda **kw: [(q.page, q.copy, q.side) for q in tp.document_sequence(p, **kw)]  # noqa: E731
    assert seq() == [(1, 1, "front"), (2, 1, "front")]  # the printer makes the 2nd copy
    assert seq(expand_copies=True) == [(1, 1, "front"), (2, 1, "front"), (1, 2, "front"), (2, 2, "front")]
    assert seq(expand_copies=True, collate=False) == [(1, 1, "front"), (1, 2, "front"), (2, 1, "front"), (2, 2, "front")]
    assert seq(expand_copies=True, duplex=True) == [(1, 1, "front"), (2, 1, "back"), (1, 2, "front"), (2, 2, "back")]


def test_render_document_records_its_sequence_and_is_deterministic():
    p = tp.PageParams(paper="a5", pages=2, copies=2)

    def src(q):  # a layout double that stamps the page / copy / side as text, so the per-page parameters are checkable
        w, h = q.size_mm()
        return {"source": "double", "elements": [{"id": "stamp", "kind": "text", "drawn": True, "primitives": [
            {"op": "text", "x": 10, "y": 20, "size_pt": 18, "text": f"{q.page}/{q.pages} {q.copy}/{q.copies} {q.side}"}]}]}

    d1 = tp.render_document(p, expand_copies=True, duplex=True, layout_source=src)
    d2 = tp.render_document(p, expand_copies=True, duplex=True, layout_source=src)
    assert d1.pdf == d2.pdf and d1.meta["sequence"][1] == [2, 1, "back"]
    assert ta.sequence_from_pdf(d1.pdf) == d1.meta["sequence"] and ta.pdf_facts(d1.pdf)["pages"] == 4


# ---- the v3 page at any sheet (no layout of ours: everything below comes from fakescan layout v3 through the adapter) -----------------

V3_SHEETS = [
    pytest.param(dict(paper="a4"), id="a4"),
    pytest.param(dict(paper="letter"), id="letter"),
    pytest.param(dict(paper="a5"), id="a5"),
    pytest.param(dict(paper="a3"), id="a3"),
    pytest.param(dict(paper="legal"), id="legal"),
    pytest.param(dict(paper="123x456"), id="custom-123x456"),
    pytest.param(dict(paper="a4", orientation="landscape"), id="a4-landscape"),
    pytest.param(dict(paper="letter", orientation="landscape"), id="letter-landscape"),
    pytest.param(dict(paper="a3", orientation="landscape", colour="mono"), id="a3-landscape-mono"),
]


@pytest.fixture
def need_v3():
    pytest.importorskip("fakescan.diag")
    if not tp.v3_available():
        pytest.skip("the installed fakescan extension has no layout v3 (rebuild fakescan-python)")


@pytest.mark.parametrize("kw", V3_SHEETS)
def test_v3_page_matches_its_layout_at_any_sheet(need_v3, kw):
    """The PDF is the layout: the sheet is exact, every sample the layout lists is what pdfium paints there, the analyzer reads upright /
    1.000 / no clipping / the page stamp, and nothing is dropped without a recorded reason."""
    p = tp.PageParams(**kw)
    w, h = p.size_mm()
    page = tp.render_pdf(p)
    lay = page.layout
    assert page.meta["mode"] == "vector" and lay["source"] == "fakescan.diag v3" and lay["sheet_mm"] == [w, h]
    facts = ta.pdf_facts(page.pdf)
    assert facts["page_boxes"][0]["size_mm"] == pytest.approx([w, h], abs=0.01) and facts["page_boxes"][0]["effective_box"] == "MediaBox"
    assert facts["images"], "raster-only elements (ramps, wheel, star, zone plate, QR) are embedded as images"
    # layout samples against the rasterised PDF (page_border hairlines sit half outside the sheet: not a sample of the sheet)
    r = ta.render_pdf_exact(page.pdf, 200)
    n = 0
    for e in lay["elements"]:
        if e["id"] == "page_border" or not e.get("drawn", True):
            continue
        for sm in e.get("samples", []):
            x, y = min(int(sm["x_mm"] / MM * 200), r.img.width - 1), min(int(sm["y_mm"] / MM * 200), r.img.height - 1)
            exp = sm["rgb"] if p.colour == "colour" else [int(0.299 * sm["rgb"][0] + 0.587 * sm["rgb"][1] + 0.114 * sm["rgb"][2] + 0.5)] * 3
            got = r.img.getpixel((x, y))
            assert max(abs(got[i] - exp[i]) for i in range(3)) <= sm["tol"], (e["id"], sm, got)
            n += 1
    assert n > 100
    assert all(e.get("drawn", True) or e.get("reason") for e in lay["elements"])
    assert {"reg_tl", "reg_tr", "reg_bl", "reg_br", "qr", "page_number", "side_marker", "orient_tl", "colorchecker24"} <= {
        e["id"] for e in lay["elements"] if e.get("drawn", True)}
    rep = ta.analyze_document(page.pdf, detail="fast")
    s = rep["summary"]
    assert rep["is_test_page"] and s["verdict_orientation"] == "upright" and s["glyph_orientation"] == "upright", rep.get("reason")
    assert s["scale_x"] == pytest.approx(1.0, abs=0.002) and s["scale_y"] == pytest.approx(1.0, abs=0.002)
    assert s["clipped_edges"] == [] and s["sheet_size_ok"] and s["sheet_mm"] == pytest.approx([w, h], abs=0.3)
    assert s["qr_ok"] and "lay=3" in s["qr_text"]
    assert (s["page"], s["page_bars"], s["side"], s["copy"], s["pages"]) == (1, 1, "front", 1, 1) and s["page_order_ok"]
    if p.colour == "colour":  # (fakescan.diag3 finds the frame as blue minus red: no frame result on a gray page)
        assert abs(s["margin_errors_mm"]["left"]) < 0.3 and abs(s["margin_errors_mm"]["bottom"]) < 0.3  # the frame is where the margin says
        assert s["colorchecker_mean_delta_e76"] < 1.5 and s["channel_order"] == "rgb"


def test_v3_is_deterministic_and_dashed_outlines_follow_the_sheet(need_v3):
    a = tp.render_pdf(tp.PageParams(paper="a3", job="x1"))
    assert a.pdf == tp.render_pdf(tp.PageParams(paper="a3", job="x1")).pdf
    assert a.pdf != tp.render_pdf(tp.PageParams(paper="a3", job="x2")).pdf  # the job tag is on the page
    drawn = lambda lay, i: next(e for e in lay["elements"] if e["id"] == i)["drawn"]  # noqa: E731
    a5 = tp.layout_for(tp.PageParams(paper="a5"))
    assert drawn(tp.layout_for(tp.PageParams(paper="a3")), "outline_a4") and not drawn(a5, "outline_a4")  # an A4 outline only where A4 fits
    assert drawn(a5, "outline_a6")


FAULTS = [("ROTATE_180", "rotated 180 degrees", False), ("FLIP_LEFT_RIGHT", "mirrored left-right", True),
          ("FLIP_TOP_BOTTOM", "flipped top-bottom", True), ("ROTATE_90", "rotated 90 degrees counter-clockwise", False),
          ("ROTATE_270", "rotated 90 degrees clockwise", False), ("TRANSPOSE", "transposed", True)]


@pytest.mark.parametrize("kw,faults", [
    (dict(paper="a4"), [f[0] for f in FAULTS]),
    (dict(paper="letter", orientation="landscape"), ["ROTATE_180", "FLIP_LEFT_RIGHT", "ROTATE_90"]),
    (dict(paper="legal"), ["ROTATE_180", "FLIP_TOP_BOTTOM"]),
    (dict(paper="a5", side="back", page=2, pages=2), ["ROTATE_180", "FLIP_LEFT_RIGHT"]),
], ids=["a4", "letter-landscape", "legal", "a5-back"])
def test_v3_orientation_faults_are_detected_at_any_sheet(need_v3, kw, faults):
    from PIL import Image

    p = tp.PageParams(**kw)
    r = ta.render_pdf_exact(tp.render_pdf(p).pdf, 150)
    for op in faults:
        want = next(f for f in FAULTS if f[0] == op)
        rep = ta.analyze_raster(_variant(r, r.img.transpose(getattr(Image.Transpose, op))), p, detail="fast")
        s = rep["summary"]
        assert rep["is_test_page"], (op, rep["reason"])
        assert s["verdict_orientation"].startswith(want[1]) and s["mirrored"] is want[2], (op, s["verdict_orientation"])
        assert not rep["orientation"]["ambiguous"]
        # after undoing the fault the page is measured as an upright 1.000 page and its stamps are read
        assert s["scale_x"] == pytest.approx(1.0, abs=0.003) and s["glyph_orientation"] == "upright" and s["page"] == p.page and s["page_order_ok"]


@pytest.mark.parametrize("op,verdict", [("ROTATE_180", "rot180"), ("FLIP_LEFT_RIGHT", "flip_h"), ("FLIP_TOP_BOTTOM", "flip_v")])
def test_v3_corner_glyphs_alone_name_the_fault(need_v3, op, verdict):
    """The layout-driven fakescan analyzer on a faulted raster, without the dihedral search: the F / R / G / J corner glyphs say which."""
    from PIL import Image

    p = tp.PageParams(paper="letter", orientation="landscape")
    page = tp.render_pdf(p)
    r = ta.render_pdf_exact(page.pdf, 150)
    d = fakescan_diag.analyze_diag_page(r.img.transpose(getattr(Image.Transpose, op)), page.layout, scan_dpi=150)
    assert d["elements"]["orientation"]["verdict"] == verdict


def test_v3_page_copy_side_stamps_are_read_back(need_v3):
    p = tp.PageParams(paper="letter", page=3, pages=5, copy=2, copies=2, side="back", job="t1")
    page = tp.render_pdf(p)
    rep = ta.analyze_document(page.pdf, detail="fast")
    s = rep["summary"]
    assert (s["page"], s["page_bars"], s["pages"], s["copy"], s["side"], s["job"]) == (3, 3, 5, 2, "back", "T1")
    assert s["page_order_ok"] and s["glyph_orientation"] == "upright"
    lay = page.layout
    sm = next(e for e in lay["elements"] if e["id"] == "side_marker")["expected"]
    assert sm["side"] == "back" and sm["label"] == "BACK" and sm["ink_half"] == "right"
    assert next(e for e in lay["elements"] if e["id"] == "orient_tl")["expected"]["mirrored"] is True  # the back mirrors the corner glyphs
    # the same page as a front side: other side marker, glyphs not mirrored
    f = tp.layout_for(p.replace(side="front"))
    assert next(e for e in f["elements"] if e["id"] == "side_marker")["expected"]["ink_half"] == "left"
    assert next(e for e in f["elements"] if e["id"] == "orient_tl")["expected"]["mirrored"] is False
    # a page whose stamp disagrees with what was expected is reported, not hidden: read 3, expected 2
    rep2 = ta.analyze_raster(ta.render_pdf_exact(page.pdf, 150), p.replace(page=2), detail="fast")
    assert rep2["summary"]["page"] == 3 and rep2["summary"]["page_order_ok"] is False


def test_v3_document_sequence_collate_and_duplex_are_visible_in_the_pages(need_v3):
    p = tp.PageParams(paper="a4", pages=2, copies=2, job="dup")
    doc = tp.render_document(p, expand_copies=True, collate=True, duplex=True)
    assert doc.meta["sequence"] == [[1, 1, "front"], [2, 1, "back"], [1, 2, "front"], [2, 2, "back"]]
    rep = ta.analyze_document(doc.pdf, params=p, pages="all", detail="fast")
    got = [(x["page"], x["copy"], x["side"]) for x in rep["pages"]]
    assert got == [(1, 1, "front"), (2, 1, "back"), (1, 2, "front"), (2, 2, "back")]
    assert all(x["is_test_page"] and x["page_order_ok"] and x["glyph_orientation"] == "upright" for x in rep["pages"])
