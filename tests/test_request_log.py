"""The raw request log (``/v1/requests``) through the embedded server and the Python models."""

from __future__ import annotations

import hashlib
import struct

import httpx
import pytest

from fakeprint import PrintTicket, Request, TicketSubstitution
from fakeprint.client import Client
from fakeprint.requestlog import Body, RequestPage

pytestmark = pytest.mark.embedded

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _ipp_print_job(uri: str, request_id: int, extra: list[bytes], data: bytes) -> bytes:
    def attr(tag: int, name: str, value: bytes) -> bytes:
        return bytes([tag]) + struct.pack(">H", len(name)) + name.encode() + struct.pack(">H", len(value)) + value

    b = bytes([2, 0, 0, 2]) + struct.pack(">I", request_id) + b"\x01"
    b += attr(0x47, "attributes-charset", b"utf-8") + attr(0x48, "attributes-natural-language", b"en")
    b += attr(0x45, "printer-uri", uri.encode()) + attr(0x42, "requesting-user-name", b"tester")
    b += attr(0x42, "job-name", b"py-ipp")
    if extra:
        b += b"\x02" + b"".join(extra)
    return b + b"\x03" + data


def test_wsd_ticket_round_trip_inline_and_mtom(srv, pc):
    ticket = PrintTicket(job_name="rawlog py", user="tester", copies=2, media_size="iso_a0_841x1189mm")
    job_inline = pc.print_document(PDF, format="application/pdf", ticket=ticket)
    job_mtom = pc.print_document(PDF, format="application/pdf", ticket=ticket, mtom=True)

    recs = srv.requests()
    assert [r.action for r in recs if r.action in ("CreatePrintJob", "SendDocument")] == [
        "CreatePrintJob",
        "SendDocument",
        "CreatePrintJob",
        "SendDocument",
    ]
    assert all(isinstance(r, Request) for r in recs)
    assert [r.seq for r in recs] == sorted(r.seq for r in recs)

    creates = srv.requests(action="CreatePrintJob")
    assert len(creates) == 2
    c = creates[0]
    assert c.direction == "in" and c.protocol == "wsd" and c.method == "POST" and c.result == "ok"
    assert c.job_id == job_inline
    assert c.ticket_received and c.ticket_received.startswith("<")
    assert "rawlog py" in c.ticket_received
    assert c.has_job_processing and c.has_document_processing
    # the media the profile does not list is reported as a substitution, not lost
    sub = c.substituted("MediaSizeName")
    assert isinstance(sub, TicketSubstitution)
    assert sub.requested == "iso_a0_841x1189mm"
    assert sub.reason

    sends = srv.requests(action="SendDocument")
    assert len(sends) == 2
    inline, mtom = sends
    assert not inline.is_mtom and mtom.is_mtom
    assert inline.content_type.startswith("application/soap+xml")
    assert inline.job_id == job_inline and mtom.job_id == job_mtom
    assert inline.body.document and mtom.body.document
    assert inline.body.length >= len(PDF)
    assert hashlib.sha256(inline.body.data).hexdigest() == inline.body.sha256 or inline.body.truncated
    assert inline.user_agent  # whatever the client says: present
    assert mtom.header("Content-Type") == mtom.content_type

    # per-job links and the stored ticket on the job
    job = srv.job(job_inline)
    assert job is not None
    assert job.ticket_received == c.ticket_received
    assert [s["field"] for s in job.ticket_substitutions] == [s.field for s in c.substitutions]
    assert set(job.request_seqs) >= {c.seq, inline.seq}
    assert [r.seq for r in srv.requests(job=job_inline)] == sorted(job.request_seqs)

    # raw bytes: wire head first, then the stored body
    raw = srv.request_raw(inline.seq)
    assert raw is not None and raw.startswith(b"POST /")
    assert b"\r\n\r\n" in raw
    assert srv.request_raw(inline.seq, part="response", body_only=True)
    assert srv.request_raw(10**9) is None
    assert srv.request(inline.seq).seq == inline.seq  # type: ignore[union-attr]
    assert srv.request(10**9) is None
    assert srv.validation_problem_count() == 0


def test_since_limit_and_page_bookkeeping(srv, pc):
    pc.get_printer_elements()
    first = srv.requests()
    assert len(first) == 1
    pc.get_printer_elements()
    pc.get_printer_elements()
    newer = srv.requests(since=first[-1].seq)
    assert len(newer) == 2
    assert len(srv.requests(limit=1)) == 1
    page = srv.requests_page(bodies=False)
    assert isinstance(page, RequestPage)
    assert page.count == 3 and page.next_seq == newer[-1].seq + 1 and page.dropped == 0
    assert page.records[0].body.data == b"" and page.records[0].body.length > 0
    assert page.config["enabled"] is True and page.config["capacity"] == 256


def test_clear_config_and_ring_overflow(srv, pc):
    cfg = srv.request_log_config(capacity=3)
    assert cfg["capacity"] == 3
    for _ in range(5):
        pc.get_printer_elements()
    page = srv.requests_page()
    assert [r.seq for r in page.records] == [3, 4, 5] and page.dropped == 2
    assert srv.clear_requests() == 3
    assert srv.requests() == []
    pc.get_printer_elements()
    assert [r.seq for r in srv.requests()] == [6]
    off = srv.request_log_config(enabled=False)
    assert off["enabled"] is False
    pc.get_printer_elements()
    assert len(srv.requests()) == 1  # nothing new while off
    assert srv.request_log_config(enabled=True, capacity=256)["enabled"] is True
    with pytest.raises(ValueError):
        srv.request_log_config(nonsense=1)


def test_injected_fault_and_drop_are_visible(srv, pc):
    srv.inject_fault("GetPrinterElements", subcode="wprt:ServerErrorTemporaryError")
    with pytest.raises(Exception):
        pc.get_printer_elements()
    recs = srv.requests(action="GetPrinterElements")
    assert recs[-1].faulted and recs[-1].fault_rule and "GetPrinterElements" in recs[-1].fault_rule
    pc.get_printer_elements()
    assert srv.requests(action="GetPrinterElements")[-1].result == "ok"


def test_ipp_print_job_is_decoded(srv):
    uri = f"ipp://127.0.0.1:{srv.port}/ipp/print"
    body = _ipp_print_job(uri, 41, [bytes([0x44]) + struct.pack(">H", 5) + b"sides" + struct.pack(">H", 4) + b"nope"], PDF)
    r = httpx.post(
        f"{srv.base_url}/ipp/print",
        content=body,
        headers={"Content-Type": "application/ipp", "SOAPAction": '""'},
    )
    assert r.status_code == 200
    (p,) = srv.requests(action="Print-Job")
    assert p.protocol == "ipp" and p.ipp_operation == "Print-Job" and p.ipp_operation_id == 2
    assert p.request_id == 41 and p.job_id and p.body.document
    assert p.soap_action == '""'
    names = [a["name"] for a in (p.ipp or {})["request_attrs"]]
    assert "job-name" in names and "sides" in names
    assert any(s.field == "sides" for s in p.substitutions)
    assert p.ipp_status and "successful" in p.ipp_status


def test_http_client_sees_the_wire_headers_as_written(srv):
    """Over a real socket: the header case and order the client wrote are kept."""
    env = b"<x/>"
    with Client(srv.base_url) as c:
        # a raw HTTP/1.1 request through httpx keeps whatever case the caller used
        r = httpx.post(
            f"{srv.base_url}/WebServices/PrinterService",
            content=env,
            headers=[("User-Agent", "Probe/9"), ("X-MiXeD-CaSe", "1"), ("Content-Type", "application/soap+xml")],
        )
        assert r.status_code >= 400  # not a valid envelope: still logged
        (rec,) = c.requests()
        assert rec.headers_raw
        assert "X-MiXeD-CaSe" in rec.header_names()
        assert rec.header("user-agent") == "Probe/9"
        assert rec.result == "faulted"
        assert rec.action is None
        raw = c.request_raw(rec.seq)
        assert raw is not None and b"X-MiXeD-CaSe: 1\r\n" in raw
        assert c.clear_requests() == 1
        assert c.requests() == []


def test_models_parse_defensively():
    r = Request.from_json({"seq": 1})
    assert r.headers == [] and r.body == Body(0, 0, False) and r.substitutions == []
    assert r.header("x") is None and not r.chunked and not r.is_mtom
    assert Body.from_json({"length": 3, "stored": 3, "truncated": False, "base64": "YWJj"}).data == b"abc"


def test_multi_device_logs_are_separate(server_cls):
    from fakeprint import Device

    s = server_cls(devices=[Device("one", "generic-wsd"), Device("two", "generic-wsd")])
    try:
        two = s.devices["two"]
        with two.printer() as pc:
            pc.get_printer_elements()
        assert len(two.requests()) == 1
        assert two.requests()[0].path.startswith("/d/two/")
        assert s.requests() == []  # the default device's log
        assert len(s.requests(device="two")) == 1
        assert s.clear_requests(device="two") == 1
    finally:
        s.stop()
