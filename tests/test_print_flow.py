"""WS-Print over HTTP against the embedded server: elements, job round trips, cancel, MTOM, faults.

Everything here runs against the Rust library's own server (``fakeprint::server::serve``). The
tests that used to be xfail until the library caught up (faults with subcodes, unknown job ids,
format/compression checks, history, multi-document capture, schema-valid GetPrinterElements, ...)
are ordinary tests now; the section of the emulator's docs/reference/ that specifies each one is in
its comment.
"""

from __future__ import annotations

import time

import pytest

from fakeprint import HttpError, JobIdNotFound, PrintTicket, SoapFault
from fakeprint.soap import PRINT_NS, PRINTER_ELEMENTS

pytestmark = pytest.mark.embedded

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
# bytes that stress a hand-rolled MIME reader: NULs, CR/LF pairs, a boundary-ish prefix, high bytes
BINARY = bytes(range(256)) * 5 + b"\r\n\r\n--not-the-boundary\x00\xff" + b"tail"


# ---- GetPrinterElements -------------------------------------------------------------------


def test_get_printer_elements_shape(pc):
    pe = pc.get_printer_elements()
    assert set(pe.items) == set(PRINTER_ELEMENTS)
    assert all(e.valid and e.element is not None for e in pe.items.values())
    assert pe.printer_state in {"Idle", "Processing", "Stopped"}
    assert pe.queued_job_count == 0
    desc = pe["PrinterDescription"]
    assert desc.text("ColorSupported") in {"true", "false"}
    assert int(desc.text("PagesPerMinute")) > 0
    assert desc.text("DeviceId", ) and "CLS:PRINTER" in desc.text("DeviceId")
    assert desc.text("PrinterName")
    assert "application/pdf" in pe.document_formats
    assert pe["PrinterStatus"].text("PrinterCurrentTime")
    assert pe["DefaultPrintTicket"].element is not None


def test_get_printer_elements_subset_and_unknown(pc):
    pe = pc.get_printer_elements(["PrinterStatus", "NoSuchSection"])
    assert pe["PrinterStatus"].valid
    assert not pe["NoSuchSection"].valid and pe["NoSuchSection"].element is None


def test_printer_elements_reply_validates(pc, real_ss):
    from fakeprint import wsd_schema as w

    pe = pc.get_printer_elements()
    resp = next(el for el in _body_children(pc.last_response))
    problems = w.validate(real_ss, resp)
    assert problems == [], problems[:5]
    assert pe.items  # reply parsed


def _body_children(xml: str):
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml)
    body = next(c for c in root if c.tag.endswith("}Body"))
    return list(body)


# ---- the happy path -----------------------------------------------------------------------


@pytest.mark.parametrize("mtom", [False, True], ids=["inline-base64", "mtom"])
def test_round_trip_byte_identical(srv, pc, mtom):
    ticket = PrintTicket(job_name="round trip", user="tester", copies=2, sides="OneSided")
    job = pc.create_print_job(ticket)
    assert job >= 1
    assert [s.state for s in pc.get_active_jobs()] == ["Pending"]
    assert pc.get_job_status(job).state == "Pending"

    pc.send_document(job, PDF, format="application/pdf", mtom=mtom, name="a.pdf")

    st = pc.get_job_status(job)
    assert st.state == "Completed"
    assert st.documents == 1
    assert pc.get_active_jobs() == []
    # the capture inbox holds exactly our bytes, on disk and through the accessor
    caps = srv.captured()
    assert [(c.job_id, c.bytes) for c in caps] == [(job, len(PDF))]
    assert srv.documents(job) == [PDF]
    assert open(caps[0].path, "rb").read() == PDF
    j = srv.job(job)
    assert (j.state, j.name, j.user, len(j.documents), j.active) == ("Completed", "round trip", "tester", 1, False)


def test_capture_records_document_format(srv, pc):
    job = pc.print_document(PDF, format="application/pdf")
    assert [c.document_format for c in srv.captured() if c.job_id == job] == ["application/pdf"]


def test_mtom_binary_payload(srv, pc):
    job = pc.print_document(BINARY, format="application/octet-stream", mtom=True)
    assert srv.documents(job) == [BINARY]


def test_mtom_large_payload(srv, pc):
    data = bytes((i * 7 + 3) % 251 for i in range(3 * 1024 * 1024))
    job = pc.print_document(data, format="application/octet-stream", mtom=True)
    assert srv.documents(job) == [data]
    assert pc.get_job_status(job).k_octets_processed == len(data) // 1024


def test_mtom_payload_containing_crlf_dashes(srv, pc):
    data = b"head\r\n--inside-the-document--\r\ntail"
    job = pc.print_document(data, format="application/octet-stream", mtom=True)
    assert srv.documents(job) == [data]


def test_multi_document_job(srv, pc):
    job = pc.create_print_job()
    pc.send_document(job, b"one", last=False, document_id=1)
    assert pc.get_job_status(job).state == "Processing"
    assert [s.job_id for s in pc.get_active_jobs()] == [job]
    pc.send_document(job, b"two", last=True, document_id=2)
    st = pc.get_job_status(job)
    assert (st.state, st.documents) == ("Completed", 2)
    assert [(c.job_id, c.bytes) for c in srv.captured()] == [(job, 3), (job, 3)]


def test_multi_document_bytes_kept_apart(srv, pc):
    job = pc.create_print_job()
    pc.send_document(job, b"one", last=False, document_id=1)
    pc.send_document(job, b"two", last=True, document_id=2)
    assert srv.documents(job) == [b"one", b"two"]


def test_job_ids_increase_and_summary_fields(pc):
    t = lambda n: PrintTicket(job_name=n, user="alice")  # noqa: E731
    a, b = pc.create_print_job(t("first")), pc.create_print_job(t("second"))
    assert b == a + 1
    summaries = {s.job_id: s for s in pc.get_active_jobs()}
    assert summaries[a].name == "first" and summaries[a].user == "alice"
    assert summaries[b].name == "second"
    assert {s.state for s in summaries.values()} == {"Pending"}


def test_job_name_with_markup_round_trips(pc):
    job = pc.create_print_job(PrintTicket(job_name="a & <b>", user="u"))
    assert [s.name for s in pc.get_active_jobs() if s.job_id == job] == ["a & <b>"]


def test_job_print_ticket_comes_back(pc):
    job = pc.create_print_job(PrintTicket(job_name="named", user="bob"))
    t = pc.get_job_ticket(job)
    assert (t.job_name, t.user) == ("named", "bob")


def test_job_elements_known_name(pc):
    job = pc.create_print_job()
    els = pc.get_job_elements(job, ["JobStatus"])
    assert els["JobStatus"].valid


def test_job_elements_unknown_name(pc):
    job = pc.create_print_job()
    els = pc.get_job_elements(job, ["JobStatus", "Bogus"])
    assert els["JobStatus"].valid and not els["Bogus"].valid


def test_ticket_with_all_settings_is_accepted(pc):
    t = PrintTicket(
        copies=3,
        collate=True,
        priority=20,
        media_size="iso_a4_210x297mm",
        media_type="stationery",
        pages_per_sheet=2,
        orientation="Landscape",
        resolution=(600, 600),
        print_quality="Normal",
        sides="TwoSidedLongEdge",
        must_honor=frozenset({"copies"}),
    )
    assert pc.create_print_job(t) >= 1


# ---- cancel -------------------------------------------------------------------------------


def test_cancel_pending_job(srv, pc):
    job = pc.create_print_job(PrintTicket(job_name="doomed"))
    pc.cancel_job(job)
    assert pc.get_active_jobs() == []
    assert pc.get_job_status(job).state == "Canceled"
    j = srv.job(job)
    assert (j.state, j.active) == ("Canceled", False)
    assert srv.captured() == []


def test_cancel_after_partial_send(srv, pc):
    job = pc.create_print_job()
    pc.send_document(job, b"part", last=False)
    pc.cancel_job(job)
    assert pc.get_job_status(job).state == "Canceled"
    # a successful cancel discards the job's captured document files (fakeprint docs/injection.md)
    assert srv.documents(job) == [] and srv.captured() == []
    assert list(srv.inbox_dir.glob("*part*")) == [] and not any(p.read_bytes() == b"part" for p in srv.inbox_dir.rglob("*") if p.is_file())


def test_device_side_cancel(srv, pc):
    job = pc.create_print_job()
    assert srv.device_cancel(job) is True
    assert srv.device_cancel(job) is False  # no longer active
    assert pc.get_job_status(job).state == "Canceled"


def test_send_after_cancel_faults(pc):
    job = pc.create_print_job()
    pc.cancel_job(job)
    with pytest.raises(SoapFault):
        pc.send_document(job, b"late")


def test_document_after_last_document(pc):
    job = pc.create_print_job()
    pc.send_document(job, b"one", last=True)
    with pytest.raises(SoapFault) as e:
        pc.send_document(job, b"two", last=True)
    assert e.value.subcode_local == "ClientErrorLastDocumentAlreadySent"


def test_completed_job_in_history(pc):
    job = pc.print_document(PDF, format="application/pdf")
    assert job in [s.job_id for s in pc.get_job_history()]


def test_completed_reason(pc):
    job = pc.print_document(PDF, format="application/pdf")
    assert "JobCompletedSuccessfully" in pc.get_job_status(job).reasons


# ---- injected faults (server-side rules) -------------------------------------------


def test_injected_fault_with_subcode(srv, pc):
    srv.inject_fault(
        "CreatePrintJob", code="Receiver", subcode="wprt:ServerErrorNotAcceptingJobs", reason="in service mode"
    )
    assert srv.pending_faults() == [("CreatePrintJob", 1)]
    with pytest.raises(SoapFault) as e:
        pc.create_print_job()
    f = e.value
    assert f.is_receiver and f.subcode == "wprt:ServerErrorNotAcceptingJobs"
    assert f.subcode_ns == PRINT_NS and f.reason == "in service mode"
    assert f.status_code == 500
    assert type(f).__name__ == "NotAcceptingJobs"
    # one-shot: the next call works, and the rejected request created no job
    assert srv.pending_faults() == []
    assert pc.create_print_job() == 1


def test_injected_sender_fault_maps_to_exception_class(srv, pc):
    srv.inject_fault("CancelJob", code="Sender", subcode="wprt:ClientErrorJobIdNotFound", reason="unknown job 9")
    with pytest.raises(JobIdNotFound) as e:
        pc.cancel_job(9)
    assert e.value.is_sender


def test_fault_counts_and_wildcard(srv, pc):
    srv.inject_fault("*", count=2, reason="flaky")
    for _ in range(2):
        with pytest.raises(SoapFault, match="flaky"):
            pc.get_active_jobs()
    assert pc.get_active_jobs() == []
    srv.inject_fault("GetActiveJobs", count=None)
    for _ in range(3):
        with pytest.raises(SoapFault):
            pc.get_active_jobs()
    assert pc.create_print_job() >= 1  # other actions unaffected
    srv.clear_faults()
    assert pc.get_active_jobs()


def test_fault_on_send_document_leaves_job_open(srv, pc):
    job = pc.create_print_job()
    srv.inject_fault("SendDocument", subcode="wprt:ServerErrorTemporaryError", reason="busy")
    with pytest.raises(SoapFault) as e:
        pc.send_document(job, PDF, format="application/pdf", mtom=True)
    assert e.value.subcode_local == "ServerErrorTemporaryError"
    assert srv.captured() == [] and srv.job(job).state == "Pending"
    pc.send_document(job, PDF, format="application/pdf", mtom=True)  # retry succeeds
    assert srv.documents(job) == [PDF]


def test_injected_http_error(srv, pc):
    srv.inject_fault("GetPrinterElements", http_status=503)
    with pytest.raises(HttpError) as e:
        pc.get_printer_elements()
    assert e.value.status_code == 503
    pc.get_printer_elements()


def test_injected_delay(srv, pc):
    srv.inject_delay("GetActiveJobs", 300, count=1)
    t0 = time.perf_counter()
    pc.get_active_jobs()
    assert time.perf_counter() - t0 >= 0.25
    t0 = time.perf_counter()
    pc.get_active_jobs()
    assert time.perf_counter() - t0 < 0.25


def test_client_timeout_against_slow_printer(srv, server_cls):
    import httpx

    srv.inject_delay("GetActiveJobs", 1500, count=1)
    slow = srv.printer(timeout=0.3)
    with pytest.raises(httpx.TimeoutException):
        slow.get_active_jobs()
    slow.close()


def test_reset_disarms_faults(srv, pc):
    srv.inject_fault("*", count=None)
    srv.reset()
    assert srv.pending_faults() == []
    assert pc.get_active_jobs() == []


# ---- real server errors -------------------------------------------------------------------


def test_unknown_action_is_a_fault(pc):
    with pytest.raises(SoapFault) as e:
        pc.call("FrobnicatePrinter", "<wprt:FrobnicatePrinterRequest/>")
    assert e.value.is_sender and "FrobnicatePrinter" in (e.value.detail or "")


def test_unknown_action_subcode(pc):
    with pytest.raises(SoapFault) as e:
        pc.call("FrobnicatePrinter", "<wprt:FrobnicatePrinterRequest/>")
    assert e.value.subcode_local == "ActionNotSupported"


def test_not_advertised_v12_operation_faults(pc):
    with pytest.raises(SoapFault):
        pc.call("SetPrinterElements", "<wprt:SetPrinterElementsRequest/>")


def test_garbage_request_is_a_fault_not_a_hang(pc):
    r = pc.raw_post(b"this is not xml")
    assert r.status_code >= 400
    assert "Fault" in r.text


def test_fault_subcode_present_on_real_faults(pc):
    with pytest.raises(SoapFault) as e:
        pc.call("FrobnicatePrinter", "<wprt:FrobnicatePrinterRequest/>")
    assert e.value.subcode is not None


def test_send_document_unknown_job(srv, pc):
    with pytest.raises(JobIdNotFound):
        pc.send_document(9999, b"x")
    assert srv.captured() == []


def test_cancel_unknown_job(pc):
    with pytest.raises(JobIdNotFound):
        pc.cancel_job(9999)


def test_get_job_elements_unknown_job(pc):
    with pytest.raises(JobIdNotFound):
        pc.get_job_elements(9999)


def test_unsupported_format(srv, pc):
    job = pc.create_print_job()
    with pytest.raises(SoapFault) as e:
        pc.send_document(job, b"x", format="application/x-not-a-real-format")
    assert e.value.subcode_local == "ClientErrorFormatNotSupported"
    assert srv.captured() == []


def test_unsupported_compression(pc):
    job = pc.create_print_job()
    with pytest.raises(SoapFault) as e:
        pc.send_document(job, b"x", format="application/pdf", compression="Gzip")
    assert e.value.subcode_local == "ClientErrorCompressionNotSupported"


def test_create_job_without_ticket_is_invalid_args(pc):
    with pytest.raises(SoapFault) as e:
        pc.call("CreatePrintJob", "<wprt:CreatePrintJobRequest/>")
    assert e.value.subcode_local == "InvalidArgs"
