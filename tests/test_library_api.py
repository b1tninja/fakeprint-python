"""What the move to the library's ``serve()`` added: every shipped profile, the library fault plan,
ticket validation, the SendDocument error matrix, job timing, events, metadata, endpoints, limits.

Every test runs against the Rust library's own embedded server. Where a reply is specified, the
emulator's docs/reference/ page is named in a comment.
"""

from __future__ import annotations

import concurrent.futures as cf
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import fakeprint
from fakeprint import ActionNotSupported, PrintTicket, SoapFault
from fakeprint.client import ApiError
from fakeprint.soap import PRINT_NS, local

pytestmark = pytest.mark.embedded

PROFILES = fakeprint.profiles()  # [] when the emulator checkout is missing: the tests then skip


def wait_for(pred, timeout: float = 5.0, step: float = 0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("timed out waiting for the condition")


class Recorder:
    """A loopback HTTP server that records every POST (a WS-Eventing NotifyTo / EndTo target)."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []
        rec = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("content-length") or 0)
                rec.posts.append((self.path, self.rfile.read(n).decode("utf-8", "replace")))
                self.send_response(200)
                self.send_header("content-length", "0")
                self.end_headers()

            def log_message(self, *a: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._httpd.server_port}"

    def actions(self, path: str = "/notify") -> list[str]:
        out = []
        for p, body in list(self.posts):
            if p == path:
                m = re.search(r"<(?:\w+:)?Action[^>]*>\s*([^<\s]+)\s*<", body)
                out.append(m.group(1).rsplit("/", 1)[-1] if m else "")
        return out

    def bodies(self, short: str, path: str = "/notify") -> list[str]:
        return [b for p, b in list(self.posts) if p == path and f"/{short}<" in b]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture()
def recorder():
    r = Recorder()
    try:
        yield r
    finally:
        r.close()


def fmt_of(srv) -> str:
    return srv.status().document_formats[0]


# ---- every shipped profile ----------------------------------------------------------------


def test_profiles_listing(server_cls):
    assert {"generic-wsd", "mono-laser", "color-inkjet-duplex", "label-a4", "multi-format", "low-toner-paper-out"} <= set(PROFILES)
    with server_cls() as s:
        assert s.profiles() == PROFILES


@pytest.mark.parametrize("profile", PROFILES)
def test_profile_boots_and_full_flow_is_schema_valid(server_cls, profile):
    with fakeprint.start(profile) as srv:
        assert srv.status().profile_id == profile
        pc = srv.printer()
        pe = pc.get_printer_elements()
        assert all(e.valid and e.element is not None for e in pe.items.values())
        assert pe.document_formats == srv.status().document_formats
        stopped = pe.printer_state == "Stopped"
        fmt = fmt_of(srv)
        job = pc.create_print_job(PrintTicket(job_name=f"flow {profile}", user="tester"))
        pc.get_active_jobs()
        pc.send_document(job, b"x" * 2000, format=fmt, name="doc")
        pc.get_job_elements(job, ["JobStatus", "PrintTicket"])
        pc.get_job_history()
        srv.device().transfer_get()
        pc.get_printer_elements()
        if not stopped:
            assert pc.get_job_status(job).state == "Completed"
            assert job in [s.job_id for s in pc.get_job_history()]
        assert srv.documents(job) == [b"x" * 2000]
        v = srv.status().validation
        assert v["enabled"] is True and v["checked"] > 0
        assert srv.validation_problems() == []
        assert srv.validation_problem_count() == 0


@pytest.mark.parametrize("profile", PROFILES)
def test_profile_printer_elements_validate_against_the_package(profile, real_ss):
    from fakeprint import wsd_schema as w

    with fakeprint.start(profile) as srv:
        pc = srv.printer()
        pc.get_printer_elements()
        import xml.etree.ElementTree as ET

        root = ET.fromstring(pc.last_response)
        body = next(c for c in root if c.tag.endswith("}Body"))
        problems = w.validate(real_ss, list(body)[0])
        assert problems == [], problems[:5]


def test_validation_can_be_switched_off(server_cls):
    with server_cls(validate_replies=False) as s:
        s.printer().get_printer_elements()
        assert s.status().validation["enabled"] is False
        assert s.validation_problem_count() == 0


def test_profile_switch_changes_formats_and_capabilities(srv):
    pc = srv.printer()
    assert "application/pdf" in pc.get_printer_elements().document_formats
    srv.set_profile("label-a4")
    pe = pc.get_printer_elements()
    assert pe.document_formats == srv.status().document_formats
    assert "application/pdf" not in pe.document_formats
    with srv.client() as c:
        assert c.set_profile("generic-wsd").profile_id == "generic-wsd"
    assert "application/pdf" in pc.get_printer_elements().document_formats
    assert srv.validation_problem_count() == 0


def test_profile_document_installed_directly(srv):
    import json

    from fakeprint import _paths

    doc = json.loads((_paths.profiles_dir() / "mono-laser.json").read_text(encoding="utf-8"))
    doc["id"] = "inline-copy"
    srv.set_profile_doc(doc)
    assert srv.status().profile_id == "inline-copy"
    with srv.client() as c:
        doc["id"] = "inline-copy-2"
        assert c.set_profile_doc(doc).profile_id == "inline-copy-2"
        with pytest.raises(ApiError):
            c.set_profile_doc({"id": "broken"})
    with pytest.raises(ValueError):
        srv.set_profile_doc({"id": "broken"})


# ---- print tickets (must-honor.md) ---------------------------------------------------------


def test_must_honor_unsupported_value_is_invalid_args(server_cls):
    with server_cls(profile="label-a4") as s:
        pc = s.printer()
        with pytest.raises(SoapFault) as e:
            pc.create_print_job(PrintTicket(media_size="na_letter_8.5x11in", must_honor={"media_size"}))
        f = e.value
        # The spec subcode is wprt:InvalidArgs (there is no ClientErrorInvalidArgs).
        assert f.is_sender and f.subcode_local == "InvalidArgs" and f.status_code == 400
        assert "MediaSizeName" in f.reason and "MustHonor" in f.reason
        with pytest.raises(SoapFault) as e:
            pc.create_print_job(PrintTicket(copies=99, must_honor={"copies"}))
        assert e.value.subcode_local == "InvalidArgs"
        assert s.jobs() == []  # a refused ticket created no job


def test_unsupported_value_without_must_honor_is_substituted(server_cls):
    with server_cls(profile="label-a4") as s:
        pc = s.printer()
        job = pc.create_print_job(PrintTicket(media_size="na_letter_8.5x11in", copies=99))
        assert job >= 1


def test_supported_must_honor_value_is_accepted(server_cls):
    with server_cls(profile="label-a4") as s:
        job = s.printer().create_print_job(PrintTicket(media_size="iso_a4_210x297mm", copies=5, must_honor={"media_size", "copies"}))
        assert job >= 1


# ---- SendDocument error matrix (faults.md: order of checks) -------------------------------


def test_send_document_error_matrix(srv, pc):
    with pytest.raises(SoapFault) as e:
        pc.send_document(4242, b"x")
    assert e.value.subcode_local == "ClientErrorJobIdNotFound" and e.value.is_sender

    job = pc.create_print_job()
    with pytest.raises(SoapFault) as e:
        pc.send_document(job, b"x", format="application/x-not-a-real-format")
    assert e.value.subcode_local == "ClientErrorFormatNotSupported"
    with pytest.raises(SoapFault) as e:
        pc.send_document(job, b"x", format="application/pdf", compression="Gzip")
    assert e.value.subcode_local == "ClientErrorCompressionNotSupported"
    assert srv.captured() == []  # none of the refused documents was kept
    assert srv.job(job).state == "Pending"  # and the job is still open for a good one

    pc.send_document(job, b"ok", format="application/pdf")
    with pytest.raises(SoapFault) as e:
        pc.send_document(job, b"again", format="application/pdf")
    assert e.value.subcode_local == "ClientErrorLastDocumentAlreadySent"
    assert srv.documents(job) == [b"ok"]


def test_send_document_to_cancelled_job(pc):
    job = pc.create_print_job()
    pc.cancel_job(job)
    with pytest.raises(SoapFault) as e:
        pc.send_document(job, b"late")
    assert e.value.subcode_local in {"ClientErrorLastDocumentAlreadySent", "ServerErrorJobCancelled", "ClientErrorJobIdNotFound"}


def test_single_document_profile_refuses_a_second_document(server_cls):
    with server_cls(profile="label-a4") as s:
        pc = s.printer()
        job = pc.create_print_job()
        with pytest.raises(SoapFault) as e:
            pc.send_document(job, b"one", format="text/plain", last=False, document_id=1)
        assert e.value.subcode_local == "ClientErrorMultipleDocumentsNotSupported"
        pc.send_document(job, b"one", format="text/plain", last=True, document_id=1)  # the single-document path still works
        assert s.documents(job) == [b"one"]


def test_multi_document_job_on_multi_format_profile(server_cls):
    with server_cls(profile="multi-format") as s:
        pc = s.printer()
        formats = s.status().document_formats[:3]
        assert len(formats) >= 2
        job = pc.create_print_job()
        for i, f in enumerate(formats, start=1):
            pc.send_document(job, f"part-{i}".encode(), format=f, last=i == len(formats), document_id=i)
        assert s.documents(job) == [f"part-{i}".encode() for i in range(1, len(formats) + 1)]
        j = s.job(job)
        assert [d.format for d in j.documents] == formats
        assert [d.id for d in j.documents] == list(range(1, len(formats) + 1))
        assert j.state == "Completed" and j.last_document_received
        assert pc.get_job_status(job).documents == len(formats)


def test_documents_per_job_limit(server_cls):
    with server_cls(limits={"max_documents_per_job": 2}) as s:
        pc = s.printer()
        job = pc.create_print_job()
        pc.send_document(job, b"1", last=False, document_id=1)
        pc.send_document(job, b"2", last=False, document_id=2)
        with pytest.raises(SoapFault):
            pc.send_document(job, b"3", last=True, document_id=3)


# ---- job state progression, timeouts, the library fault plan --------------------------------


def test_job_state_progression_with_processing_time(server_cls):
    with server_cls(timing={"processing_base_ms": 600}, tick_ms=50) as s:
        pc = s.printer()
        job = pc.create_print_job()
        assert pc.get_job_status(job).state == "Pending"
        pc.send_document(job, b"data", format="application/pdf")
        assert pc.get_job_status(job).state in {"Processing", "Started"}
        assert [x.job_id for x in pc.get_active_jobs()] == [job]
        assert job not in [x.job_id for x in pc.get_job_history()]
        wait_for(lambda: pc.get_job_status(job).state == "Completed")
        st = pc.get_job_status(job)
        assert "JobCompletedSuccessfully" in st.reasons and st.media_sheets_completed >= 1
        assert pc.get_active_jobs() == []
        assert [x.job_id for x in pc.get_job_history()] == [job]


def test_document_timeout_aborts_a_job_that_gets_no_document(server_cls):
    with server_cls(timing={"document_timeout_ms": 300}, tick_ms=50) as s:
        pc = s.printer()
        job = pc.create_print_job()
        wait_for(lambda: pc.get_job_status(job).state == "Aborted")
        assert pc.get_job_status(job).reasons == ["DocumentTimeoutError"]
        assert job in [x.job_id for x in pc.get_job_history()]
        with pytest.raises(SoapFault):
            pc.send_document(job, b"too late")


def test_fault_document_active_then_clear(srv, pc):
    job = pc.create_print_job()
    state = srv.faults(document_active=True)
    assert state["active"]["document_active"] is True
    with pytest.raises(SoapFault) as e:
        pc.send_document(job, b"x")
    assert e.value.subcode_local == "ServerErrorDocumentActive" and e.value.is_receiver
    assert srv.captured() == []
    srv.faults(document_active=False)
    pc.send_document(job, b"x")
    assert srv.job(job).state == "Completed"


def test_fault_paper_out_stops_the_printer_and_holds_the_job(srv, pc):
    job = pc.create_print_job()
    st = srv.faults(paper_out=True)
    assert st["active"]["printer_state"] == "Stopped"
    assert any(c["name"] == "InputTraySupplyEmpty" for c in st["active"]["conditions"])
    assert pc.get_printer_elements().printer_state == "Stopped"
    pc.send_document(job, b"x")
    assert srv.job(job).state == "ProcessingStopped"
    assert srv.job(job).reasons == ["PrinterStopped"]
    assert [x.job_id for x in pc.get_active_jobs()] == [job]
    srv.faults(paper_out=False)
    assert pc.get_printer_elements().printer_state == "Idle"
    wait_for(lambda: pc.get_job_status(job).state == "Completed")
    assert srv.documents(job) == [b"x"]
    assert srv.validation_problem_count() == 0


def test_fault_flags_for_job_creation(srv, pc):
    srv.faults(not_accepting_jobs=True)
    with pytest.raises(fakeprint.NotAcceptingJobs):
        pc.create_print_job()
    srv.faults(not_accepting_jobs=False, temporary_error=True)
    with pytest.raises(SoapFault) as e:
        pc.create_print_job()
    assert e.value.subcode_local == "ServerErrorTemporaryError"
    srv.faults(temporary_error=False, internal_error=True)
    with pytest.raises(SoapFault) as e:
        pc.create_print_job()
    assert e.value.subcode_local == "ServerErrorInternalError"
    srv.clear_faults()
    assert pc.create_print_job() >= 1
    assert srv.faults()["active"]["internal_error"] is False


def test_fail_next_document_aborts_the_job(srv, pc):
    srv.faults(fail_next_document="Format")
    assert srv.faults()["pending"]["fail_next_document"] == "Format"
    job = pc.print_document(b"x", format="application/pdf")
    st = pc.get_job_status(job)
    assert st.state == "Aborted" and st.reasons == ["DocumentFormatError"]
    job2 = pc.print_document(b"y", format="application/pdf")  # one-shot
    assert pc.get_job_status(job2).state == "Completed"


def test_conditions_by_name_raise_and_clear(srv, pc):
    srv.raise_condition("CoverOpen")
    conds = {c["name"] for c in srv.faults()["active"]["conditions"]}
    assert "CoverOpen" in conds
    assert pc.get_printer_elements().printer_state == "Stopped"
    srv.raise_condition("Jam", component="Mechanism", severity="Critical")
    srv.clear_condition("CoverOpen")
    assert "CoverOpen" not in {c["name"] for c in srv.faults()["active"]["conditions"]}
    srv.faults(reset=True)
    assert srv.faults()["active"]["conditions"] == []
    assert pc.get_printer_elements().printer_state == "Idle"
    assert srv.validation_problem_count() == 0


def test_faults_over_http_match_the_native_call(srv):
    with srv.client() as c:
        a = c.set_faults(toner_low=True)
        assert any(x["name"] for x in a["active"]["conditions"])
        assert c.faults() == srv.faults()
        c.set_faults(raise_=[{"name": "CoverOpen"}])
        assert c.ctl("faults")["active"]["printer_state"] == "Stopped"
        assert c.clear_faults()["active"]["conditions"] == []
    with pytest.raises(ValueError):
        srv.faults(paper_out="yes")  # not a bool: the library answers 4xx, the binding raises


def test_reset_restores_conditions_and_forgets_jobs(srv, pc):
    pc.print_document(b"x")
    srv.faults(cover_open=True)
    srv.reset()
    assert srv.jobs() == [] and srv.captured() == []
    assert pc.get_printer_elements().printer_state in {"Idle", "Stopped"}


# ---- job listing -----------------------------------------------------------------------------


def test_active_and_history_listing(srv, pc):
    a = pc.create_print_job(PrintTicket(job_name="active"))
    b = pc.print_document(b"b", format="application/pdf", ticket=PrintTicket(job_name="done"))
    c = pc.create_print_job(PrintTicket(job_name="cancelled"))
    pc.cancel_job(c)
    assert [(s.job_id, s.state) for s in pc.get_active_jobs()] == [(a, "Pending")]
    hist = {s.job_id: s.state for s in pc.get_job_history()}
    assert hist == {b: "Completed", c: "Canceled"}
    # the control plane lists both, ascending, with the same states
    rows = srv.jobs()
    assert [(j.job_id, j.state) for j in rows] == [(a, "Pending"), (b, "Completed"), (c, "Canceled")]
    assert [j.active for j in rows] == [True, False, False]
    with srv.client() as cl:
        assert [j.job_id for j in cl.print_jobs()] == [a, b, c]
        one = cl.job(b)
        assert one is not None and one.name == "done" and one.documents[0].bytes == 1
        assert cl.job(9999) is None
        assert len(cl.jobs()) == 1  # captured documents only
    assert srv.status().active_jobs == 1


def test_history_is_bounded(server_cls):
    with server_cls(limits={"history_cap": 3}) as s:
        pc = s.printer()
        ids = [pc.print_document(b"x") for _ in range(6)]
        assert [x.job_id for x in pc.get_job_history()] == ids[-3:]


def test_active_job_cap(server_cls):
    with server_cls(limits={"max_active_jobs": 2}) as s:
        pc = s.printer()
        pc.create_print_job()
        pc.create_print_job()
        with pytest.raises(SoapFault) as e:
            pc.create_print_job()
        assert e.value.subcode_local == "ServerErrorNotAcceptingJobs"


# ---- WS-Eventing, end to end against a loopback recorder -------------------------------------


def test_event_subscription_delivers_job_end_state(server_cls, recorder):
    with server_cls(event_rate_secs=1, tick_ms=50) as s:
        pc = s.printer()
        sub = pc.subscribe(recorder.url + "/notify", actions=["JobEndStateEvent"], end_to=recorder.url + "/end")
        assert sub.identifier and sub.expires
        assert s.status().subscriptions == 1
        assert pc.subscription_status(sub)
        assert pc.renew(sub, "PT10M")
        job = pc.print_document(b"abc", format="application/pdf", ticket=PrintTicket(job_name="evt"))
        wait_for(lambda: recorder.bodies("JobEndStateEvent"))
        body = recorder.bodies("JobEndStateEvent")[0]
        assert f"<wprt:JobId>{job}</wprt:JobId>" in body
        assert "<wprt:JobCompletedState>Completed</wprt:JobCompletedState>" in body
        assert "JobCompletedSuccessfully" in body and "<wprt:JobName>evt</wprt:JobName>" in body
        assert set(recorder.actions()) == {"JobEndStateEvent"}  # the filter kept other events out
        pc.unsubscribe(sub)
        assert s.status().subscriptions == 0
        with pytest.raises(SoapFault):
            pc.subscription_status(sub)
        assert s.validation_problem_count() == 0


def test_event_subscription_status_summary_follows_conditions(server_cls, recorder):
    with server_cls(event_rate_secs=1, tick_ms=50) as s:
        pc = s.printer()
        pc.subscribe(recorder.url + "/notify", actions=["PrinterStatusSummaryEvent"])
        s.faults(paper_out=True)
        wait_for(lambda: any("Stopped" in b for b in recorder.bodies("PrinterStatusSummaryEvent")))
        s.faults(paper_out=False)
        wait_for(lambda: any("Idle" in b for b in recorder.bodies("PrinterStatusSummaryEvent")[1:]))
        assert s.validation_problem_count() == 0


def test_subscription_end_is_sent_when_the_server_stops(server_cls, recorder):
    s = server_cls(event_rate_secs=1, tick_ms=50)
    try:
        s.printer().subscribe(recorder.url + "/notify", end_to=recorder.url + "/end")
    finally:
        s.stop()
    ends = recorder.bodies("SubscriptionEnd", "/end")
    assert ends and "SourceShuttingDown" in ends[0]


def test_initial_events_seed_a_new_subscriber(server_cls, recorder):
    with server_cls(initial_events=True, event_rate_secs=1, tick_ms=50) as s:
        s.printer().subscribe(recorder.url + "/notify", actions=["PrinterStatusSummaryEvent"])
        wait_for(lambda: recorder.bodies("PrinterStatusSummaryEvent"))


def test_subscribe_to_a_public_address_is_refused(srv, pc):
    with pytest.raises(SoapFault):
        pc.subscribe("http://8.8.8.8:9/notify")  # PrivateOnly policy: never connects out
    assert srv.status().subscriptions == 0


def test_subscribe_on_the_device_endpoint_is_action_not_supported(srv):
    with pytest.raises(ActionNotSupported):
        srv.device().subscribe("http://127.0.0.1:9/notify")


# ---- metadata and endpoint kinds -------------------------------------------------------------


@pytest.mark.parametrize("which", ["device", "printer"])
def test_transfer_get_and_get_metadata(srv, which):
    pc = getattr(srv, which)()
    for call in (pc.transfer_get, pc.get_metadata):
        meta = call()
        assert local(meta.tag) == "Metadata"
        text = pc.last_response
        assert srv.service_endpoint in text and srv.device_uuid in text
        assert "PrinterServiceType" in text and "ThisModel" in text


def test_get_metadata_single_dialect(srv):
    pc = srv.device()
    meta = pc.get_metadata("http://schemas.xmlsoap.org/ws/2006/02/devprof/ThisModel")
    assert local(meta.tag) == "Metadata"
    assert srv.validation_problem_count() == 0


def test_print_operation_on_the_device_endpoint_is_action_not_supported(srv):
    dev = srv.device()
    with pytest.raises(ActionNotSupported) as e:
        dev.create_print_job()
    assert e.value.is_sender and e.value.subcode_ns and e.value.subcode_local == "ActionNotSupported"
    with pytest.raises(ActionNotSupported):
        dev.get_printer_elements()
    assert srv.jobs() == []


def test_legacy_print_service_path(srv):
    pc = srv.printer(endpoint=f"{srv.base_url}/PrintService")
    assert pc.get_printer_elements().printer_state == "Idle"


def test_wsd_can_be_switched_off(server_cls):
    with server_cls(wsd=False) as s:
        r = httpx.post(s.service_endpoint, content=b"<x/>")
        assert r.status_code == 404
        assert s.client().health()


def test_ipp_endpoint_answers_when_enabled(server_cls):
    with server_cls() as on, server_cls(ipp=False) as off:
        assert httpx.get(f"{on.base_url}/ipp/print").status_code != 404
        assert httpx.get(f"{off.base_url}/ipp/print").status_code == 404


def _ipp_print_job(srv, job_attrs: bytes, doc: bytes) -> bytes:
    import struct

    def attr(tag, name, val):
        return bytes([tag]) + struct.pack(">H", len(name)) + name.encode() + struct.pack(">H", len(val)) + val

    ops = attr(0x47, "attributes-charset", b"utf-8") + attr(0x48, "attributes-natural-language", b"en")
    ops += attr(0x45, "printer-uri", srv.default_device.ipp_endpoint.encode()) + attr(0x49, "document-format", b"application/pdf")
    body = struct.pack(">BBHI", 2, 0, 2, 1) + b"" + ops + b"" + job_attrs + b"" + doc
    r = httpx.post(f"{srv.base_url}/ipp/print", content=body, headers={"Content-Type": "application/ipp"})
    assert r.status_code == 200
    return r.content


def test_print_scaling_is_a_profile_knob_and_the_job_row_reports_the_scale(server_cls):
    pdf = b"%PDF-1.4\n<< /Type /Page /MediaBox [0 0 595.28 841.89] >>\n%%EOF\n"
    kw = lambda n, v: bytes([0x44]) + len(n).to_bytes(2, "big") + n.encode() + len(v).to_bytes(2, "big") + v.encode()  # noqa: E731
    letter = kw("media", "na_letter_8.5x11in") + kw("print-scaling", "auto-fit")
    with server_cls(profile="multi-format") as on:
        _ipp_print_job(on, letter, pdf)
        (job,) = on.jobs()
        assert job.print_scaling == "auto-fit" and job.ipp["attributes"]["print-scaling"] == "auto-fit"
        assert job.effective_scale == pytest.approx(279.4 / 297.0, abs=5e-4)
    with server_cls(profile="minimal-ipp") as off:
        reply = _ipp_print_job(off, letter, pdf)
        assert int.from_bytes(reply[2:4], "big") == 0x0001, "print-scaling is not advertised: accepted, the attribute reported as unsupported"
        (job,) = off.jobs()
        assert job.print_scaling is None and "print-scaling" not in job.ipp["attributes"]
        assert job.effective_scale == 1.0


# ---- size, limits and concurrency ------------------------------------------------------------


def test_five_mib_mtom_document(srv, pc):
    data = bytes((i * 31 + 7) % 253 for i in range(5 * 1024 * 1024))
    job = pc.print_document(data, format="application/octet-stream", mtom=True)
    assert srv.documents(job) == [data]
    assert pc.get_job_status(job).k_octets_processed == 5 * 1024
    assert srv.job(job).documents[0].bytes == len(data)
    assert srv.validation_problem_count() == 0


def test_five_mib_inline_base64_document(srv, pc):
    data = bytes(range(256)) * (5 * 4096)
    job = pc.print_document(data, format="application/octet-stream")
    assert srv.documents(job) == [data]


def test_document_over_the_limit_is_refused(server_cls):
    with server_cls(limits={"max_document": 1024}) as s:
        pc = s.printer()
        job = pc.create_print_job()
        with pytest.raises(SoapFault) as e:
            pc.send_document(job, b"z" * 4096, format="application/octet-stream", mtom=True)
        assert e.value.subcode_local == "InvalidArgs"
        assert s.captured() == []


def test_body_over_the_limit_is_413(server_cls):
    with server_cls(limits={"max_body": 8 * 1024, "max_document": 8 * 1024}) as s:
        pc = s.printer()
        job = pc.create_print_job()
        with pytest.raises((SoapFault, fakeprint.HttpError)) as e:
            pc.send_document(job, b"z" * (64 * 1024), format="application/octet-stream", mtom=True)
        assert e.value.status_code == 413


def test_two_servers_run_concurrently_with_different_profiles():
    with fakeprint.start("mono-laser") as a, fakeprint.start("label-a4") as b:
        assert a.port != b.port and a.status().profile_id == "mono-laser" and b.status().profile_id == "label-a4"

        def work(srv, n):
            pc = srv.printer()
            fmt = fmt_of(srv)
            return [(j, srv.documents(j)) for j in (pc.print_document(f"{srv.port}-{i}".encode(), format=fmt) for i in range(n))]

        with cf.ThreadPoolExecutor(4) as ex:
            fa, fb = ex.submit(work, a, 10), ex.submit(work, b, 10)
            ra, rb = fa.result(), fb.result()
        assert [d for _, d in ra] == [[f"{a.port}-{i}".encode()] for i in range(10)]
        assert [d for _, d in rb] == [[f"{b.port}-{i}".encode()] for i in range(10)]
        assert a.validation_problem_count() == 0 and b.validation_problem_count() == 0


def test_24_threads_one_server(srv):
    def one(i):
        pc = srv.printer()
        data = f"thread-{i}".encode() * (20 + i)
        job = pc.print_document(data, format="application/octet-stream", mtom=bool(i % 2))
        return i, data, job, pc.get_job_status(job).state

    with cf.ThreadPoolExecutor(24) as ex:
        results = list(ex.map(one, range(24)))
    assert len({j for _, _, j, _ in results}) == 24
    for _, data, job, state in results:
        assert srv.documents(job) == [data] and state == "Completed"
    assert len(srv.jobs()) == 24 and len(srv.captured()) == 24
    assert srv.validation_problem_count() == 0


# ---- the in-process control path and the client-side fault layer -----------------------------


def test_native_control_equals_http_control(srv, pc):
    pc.print_document(b"abc")
    status, doc = srv.control("GET", "/v1/jobs")
    assert status == 200
    with srv.client() as c:
        assert c._req("GET", "/v1/jobs").json() == doc
    assert srv.control("GET", "/v1/jobs/9999")[0] == 404
    assert srv.control("POST", "/v1/profile", {"id": "no-such"})[0] == 400


def test_print_jobs_json_matches_jobs(srv, pc):
    import json

    job = pc.print_document(b"abc")
    rows = json.loads(srv.print_jobs_json())
    assert [r["id"] for r in rows] == [job] == [j.job_id for j in srv.jobs()]


def test_injected_faults_reach_clients_built_elsewhere(srv):
    # rules live in the server, so a client constructed by hand (or pdfcraft's) sees them too
    srv.inject_fault("*", count=None)
    plain = fakeprint.PrintClient(srv.service_endpoint)
    with pytest.raises(SoapFault):
        plain.get_active_jobs()
    plain.close()
    with pytest.raises(SoapFault):
        srv.printer().get_active_jobs()
    srv.clear_faults()
    assert srv.printer().get_active_jobs() == []


def test_injected_fault_envelope_resolves_wprt_prefix(srv):
    srv.inject_fault("GetActiveJobs", code="Sender", subcode="wprt:ClientErrorJobIdNotFound", reason="r&d <x>")
    with pytest.raises(fakeprint.JobIdNotFound) as e:
        srv.printer().get_active_jobs()
    assert e.value.subcode_ns == PRINT_NS and e.value.reason == "r&d <x>" and e.value.status_code == 400
