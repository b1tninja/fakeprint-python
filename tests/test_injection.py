"""Server-side injection rules (POST /v1/faults "inject"), runtime timing, and profile boot.

The rules live in the Rust server, so every client sees them: this package's PrintClient, a
hand-built client and plain ``httpx`` alike.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest

import fakeprint
from fakeprint import (
    ActionNotSupported,
    HttpError,
    JobIdNotFound,
    NotAcceptingJobs,
    PrintClient,
    PrintTicket,
    SoapFault,
)
from fakeprint.soap import PRINT_NS

pytestmark = pytest.mark.embedded

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
SEND_URI = PRINT_NS + "/SendDocument"


def _until(pred, secs: float = 5.0) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# ---- effects ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code, subcode, local, sender",
    [
        ("Receiver", "wprt:ServerErrorTemporaryError", "ServerErrorTemporaryError", False),
        ("Receiver", "wprt:ServerErrorNotAcceptingJobs", "ServerErrorNotAcceptingJobs", False),
        ("Sender", "wprt:ClientErrorJobIdNotFound", "ClientErrorJobIdNotFound", True),
        ("Sender", "wprt:ClientErrorInvalidArgs", "ClientErrorInvalidArgs", True),
        ("Sender", "wsa:ActionNotSupported", "ActionNotSupported", True),
        ("Receiver", "InternalError", "InternalError", False),  # bare catalogue name
        ("Receiver", "{urn:example:x}Custom", "Custom", False),  # Clark notation
    ],
)
def test_fault_effect_each_subcode_kind(srv, pc, code, subcode, local, sender):
    srv.inject_fault("GetActiveJobs", code=code, subcode=subcode, reason="because")
    with pytest.raises(SoapFault) as e:
        pc.get_active_jobs()
    f = e.value
    assert f.subcode_local == local and f.reason == "because"
    assert f.is_sender is sender and f.is_receiver is not sender
    assert pc.get_active_jobs() == []  # count defaulted to 1
    assert srv.validation_problem_count() == 0


def test_fault_default_status_follows_code(srv, pc):
    srv.inject_fault("GetActiveJobs", code="Sender", subcode="wprt:ClientErrorInvalidArgs")
    with pytest.raises(SoapFault) as e:
        pc.get_active_jobs()
    assert e.value.status_code == 400
    srv.inject_fault("GetActiveJobs", code="Receiver", subcode="wprt:ServerErrorTemporaryError", http_status=503)
    with pytest.raises(SoapFault) as e:
        pc.get_active_jobs()
    assert e.value.status_code == 503 and e.value.subcode_local == "ServerErrorTemporaryError"


def test_fault_detail_and_exception_classes(srv, pc):
    srv.inject_fault("CancelJob", code="Sender", subcode="wprt:ClientErrorJobIdNotFound", detail="job 9")
    with pytest.raises(JobIdNotFound) as e:
        pc.cancel_job(9)
    assert "job 9" in pc.last_response
    srv.inject_fault("CreatePrintJob", subcode="wprt:ServerErrorNotAcceptingJobs")
    with pytest.raises(NotAcceptingJobs):
        pc.create_print_job()
    srv.inject_fault("GetActiveJobs", code="Sender", subcode="wsa:ActionNotSupported")
    with pytest.raises(ActionNotSupported):
        pc.get_active_jobs()


def test_bare_http_503_with_body(srv, pc):
    srv.inject_fault("GetPrinterElements", http_status=503, body="<html>down</html>", content_type="text/html")
    with pytest.raises(HttpError) as e:
        pc.get_printer_elements()
    assert e.value.status_code == 503 and "down" in e.value.body
    pc.get_printer_elements()


def test_close_connection(srv, pc):
    srv.inject_fault("GetActiveJobs", close=True)
    with pytest.raises(httpx.TransportError):
        pc.get_active_jobs()
    assert pc.get_active_jobs() == []
    # a plain client sees the same
    srv.inject_fault("GetActiveJobs", close=True)
    with pytest.raises(httpx.TransportError):
        PrintClient(srv.service_endpoint).get_active_jobs()


def test_delay_then_normal_answer(srv, pc):
    srv.inject_delay("GetActiveJobs", 300, count=1)
    t0 = time.perf_counter()
    assert pc.get_active_jobs() == []
    assert time.perf_counter() - t0 >= 0.25
    t0 = time.perf_counter()
    pc.get_active_jobs()
    assert time.perf_counter() - t0 < 0.25


def test_delay_beyond_client_timeout(srv):
    srv.inject_delay("GetActiveJobs", 1500, count=1)
    slow = srv.printer(timeout=0.3)
    with pytest.raises(httpx.TimeoutException):
        slow.get_active_jobs()
    slow.close()


def test_delay_after_processing(srv, pc):
    # delay_after: the job is created before the reply is held back
    srv.inject_delay("CreatePrintJob", 400, count=1, after=True)
    t0 = time.perf_counter()
    assert pc.create_print_job() == 1
    assert time.perf_counter() - t0 >= 0.35


def test_faulted_send_document_never_reaches_the_job_engine(srv, pc):
    job = pc.create_print_job()
    srv.inject_fault("SendDocument", subcode="wprt:ServerErrorTemporaryError")
    with pytest.raises(SoapFault):
        pc.send_document(job, PDF, format="application/pdf")
    assert srv.captured() == [] and srv.job(job).state == "Pending"
    pc.send_document(job, PDF, format="application/pdf")
    assert srv.documents(job) == [PDF]


# ---- matching -----------------------------------------------------------------------------


def test_skip_then_count(srv, pc):
    srv.inject_fault("GetActiveJobs", skip=1, count=2, reason="r")
    pc.get_active_jobs()  # skipped
    for _ in range(2):
        with pytest.raises(SoapFault, match="r"):
            pc.get_active_jobs()
    pc.get_active_jobs()
    assert srv.pending_faults() == []
    assert srv.inject_stats() == {"hits": 2, "skipped": 1}


def test_count_none_stays_until_cleared(srv, pc):
    srv.inject_fault("GetActiveJobs", count=None)
    for _ in range(4):
        with pytest.raises(SoapFault):
            pc.get_active_jobs()
    assert srv.pending_faults() == [("GetActiveJobs", None)]
    srv.clear_faults()
    assert srv.pending_faults() == [] and pc.get_active_jobs() == []


def test_op_short_name_case_and_action_uri(srv, pc):
    srv.inject_fault("getactivejobs")
    with pytest.raises(SoapFault):
        pc.get_active_jobs()
    uri = PRINT_NS + "/GetActiveJobs"
    srv.inject_fault(uri)
    with pytest.raises(SoapFault):
        pc.get_active_jobs()
    srv.inject_fault(SEND_URI)
    job = pc.create_print_job()  # other op unaffected
    with pytest.raises(SoapFault):
        pc.send_document(job, b"x")
    assert srv.pending_faults() == []


def test_wildcard_first_matching_rule_decides(srv, pc):
    srv.inject_fault("*", count=2, reason="flaky")
    srv.inject_fault("GetActiveJobs", reason="second")
    for _ in range(2):
        with pytest.raises(SoapFault, match="flaky"):
            pc.get_active_jobs()
    with pytest.raises(SoapFault, match="second"):
        pc.get_active_jobs()
    assert pc.get_active_jobs() == []


def test_endpoint_filter(srv):
    srv.inject_fault("GetPrinterElements", endpoint="device", count=None)
    srv.printer().get_printer_elements()  # the PrinterService endpoint is untouched
    with pytest.raises(SoapFault):
        srv.device().get_printer_elements()
    srv.clear_faults()


def test_rules_do_not_touch_control_plane(srv):
    srv.inject_fault("*", count=None)
    srv.inject_fault("*", http_status=503, count=None)
    with srv.client() as c:
        assert c.health() and c.status().profile_id == "generic-wsd"
    assert httpx.get(srv.base_url + "/health").text.strip() == "ok"


def test_malformed_rule_is_refused_and_changes_nothing(srv):
    with pytest.raises(ValueError, match="400"):
        srv.inject({"effect": {"type": "explode"}})
    with pytest.raises(ValueError):
        srv.inject({"count": 0})
    with pytest.raises(ValueError):
        srv.inject({"effect": {"type": "fault", "subcode": "nope:Bad"}})
    assert srv.pending_faults() == []


def test_unknown_fail_next_document_is_400(srv):
    with pytest.raises(ValueError, match="400"):
        srv.faults(fail_next_document="Bogus")


def test_inject_dict_and_client_api(srv, pc):
    srv.inject({"op": "GetActiveJobs", "endpoint": "print_service", "effect": {"type": "http", "status": 502}})
    with srv.client() as c:
        rules = c.pending_inject()
        assert rules[0]["op"] == "GetActiveJobs" and rules[0]["remaining"] == 1 and rules[0]["hits"] == 0
        c.inject(op="CancelJob", effect={"type": "fault", "code": "sender"}, count=None)
        assert [r["op"] for r in c.pending_inject()] == ["GetActiveJobs", "CancelJob"]
        c.clear_inject()
        assert c.pending_inject() == []
        c.inject({"op": "X"}, clear=True)
        assert [r["op"] for r in c.faults()["inject"]["pending"]] == ["X"]
        with pytest.raises(fakeprint.ApiError):
            c.inject({"count": 0})


def test_reset_and_delete_clear_rules(srv, pc):
    srv.inject_fault("*", count=None)
    srv.reset()
    assert srv.pending_faults() == [] and pc.get_active_jobs() == []
    srv.inject_fault("*", count=None)
    with srv.client() as c:
        c.clear_faults()
    assert pc.get_active_jobs() == []


def test_concurrent_requests_each_consume_one_slot(srv):
    srv.inject_fault("GetActiveJobs", count=5)
    results: list[bool] = []

    def one() -> None:
        c = srv.printer()
        try:
            c.get_active_jobs()
            results.append(True)
        except SoapFault:
            results.append(False)

    ts = [threading.Thread(target=one) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(False) == 5 and results.count(True) == 7
    assert srv.inject_stats()["hits"] == 5


# ---- timing -------------------------------------------------------------------------------


def test_timing_get_and_patch(srv):
    t = srv.timing()
    assert t["processing_base_ms"] == 0
    t = srv.set_timing(processing_base_ms=123, bytes_per_sheet=1000)
    assert t["processing_base_ms"] == 123 and srv.timing()["bytes_per_sheet"] == 1000
    assert srv.set_timing("realistic")["processing_base_ms"] == 2000
    assert srv.set_timing("instant")["processing_base_ms"] == 0
    with pytest.raises(ValueError):
        srv.set_timing("warp")
    with pytest.raises(ValueError):
        srv.timing(nope=1)


def test_timing_via_client(srv):
    with srv.client() as c:
        assert c.timing()["processing_base_ms"] == 0
        assert c.timing(preset="realistic")["processing_base_ms"] == 2000


def test_constructor_accepts_presets(server_cls):
    with server_cls(timing="realistic") as s:
        assert s.timing()["processing_base_ms"] == 2000
    with server_cls(timing={"preset": "realistic", "processing_base_ms": 700}) as s:
        assert s.timing()["processing_base_ms"] == 700
    with server_cls(timing="default") as s:
        assert s.timing()["processing_base_ms"] > 0
    with server_cls(timing="instant") as s:
        assert s.timing()["processing_base_ms"] == 0
    with pytest.raises(ValueError):
        server_cls(timing="warp")


def test_timing_applies_to_later_documents_only(srv, pc):
    fast = pc.print_document(b"a")
    assert pc.get_job_status(fast).state == "Completed"
    srv.set_timing(processing_base_ms=600)
    slow = pc.print_document(b"b")
    assert pc.get_job_status(slow).state == "Processing"
    assert pc.get_job_status(fast).state == "Completed"
    deadline = time.time() + 5
    while pc.get_job_status(slow).state != "Completed" and time.time() < deadline:
        time.sleep(0.1)
    assert pc.get_job_status(slow).state == "Completed"


def test_mid_job_cancel_with_slow_preset(server_cls):
    with server_cls(timing="realistic", tick_ms=50) as s:
        pc = s.printer()
        job = pc.print_document(b"x" * 2048, format="application/octet-stream")
        assert pc.get_job_status(job).state == "Processing"
        pc.cancel_job(job)
        assert pc.get_job_status(job).state in {"Terminating", "Canceled"}  # 0.5 s terminating phase
        assert _until(lambda: pc.get_job_status(job).state == "Canceled")
        assert s.captured() == [] and s.documents(job) == []  # captured files discarded
        assert s.job(job).state == "Canceled"
        time.sleep(0.3)
        assert s.job(job).state == "Canceled"  # the processing timer does not resurrect it
        assert s.validation_problem_count() == 0


def test_device_cancel_mid_job(server_cls):
    with server_cls(timing="realistic", tick_ms=50) as s:
        pc = s.printer()
        job = pc.print_document(b"y" * 100)
        assert s.device_cancel(job) is True
        assert _until(lambda: s.job(job).state == "Canceled")


# ---- profiles -----------------------------------------------------------------------------


@pytest.mark.parametrize("profile", fakeprint.profiles())
def test_every_shipped_profile_boots_and_prints(server_cls, profile):
    with server_cls(profile=profile) as s:
        assert s.status().profile_id == profile
        pc = s.printer()
        pe = pc.get_printer_elements()
        assert pe.items and pe.document_formats
        job = pc.print_document(b"hello", format=pe.document_formats[0])
        # low-toner-paper-out boots with the printer stopped: the job is held
        assert pc.get_job_status(job).state in {"Completed", "Pending", "Processing", "ProcessingStopped"}
        assert s.validation_problem_count() == 0, s.validation_problems()


def test_minimal_monochrome_profile_loads(srv):
    srv.set_profile_doc(
        {"schema_version": 2, "id": "mono-min", "label": "Mono", "description": {"color_modes": ["monochrome"]}}
    )
    assert srv.status().profile_id == "mono-min"
    pe = srv.printer().get_printer_elements()
    assert pe["PrinterDescription"].text("ColorSupported") == "false"
    assert srv.validation_problem_count() == 0
    # the smallest document of all
    srv.set_profile_doc({"schema_version": 2, "id": "smallest", "label": "Smallest"})
    assert srv.status().profile_id == "smallest"
    assert srv.validation_problem_count() == 0


def test_device_cancel_reports_canceled_at_device_and_discards_files(server_cls):
    with server_cls(timing={"processing_base_ms": 5000, "terminating_ms": 50}) as s:
        pc = s.printer()
        job = pc.print_document(b"z" * 10)
        assert s.device_cancel(job) is True
        assert _until(lambda: s.job(job).state == "Canceled")
        assert "JobCanceledAtDevice" in s.job(job).reasons
        assert s.captured() == [] and s.device_cancel(job) is False
