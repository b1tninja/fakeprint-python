"""Embedded multi-device servers (real native module, loopback, port 0, no Windows, no discovery).

Skipped, with the reason, while the installed native module or the fakeprint library behind it cannot
serve more than one device (the tests then still document what is expected).
"""

from __future__ import annotations

import re
import time

import httpx
import pytest

from fakeprint import Device, Server
from fakeprint.client import ApiError
from fakeprint.manager import Conflict, NotYet, UnknownDevice
from fakeprint.soap import SoapFault

pytestmark = pytest.mark.embedded

PDF = b"%PDF-1.4 multi"


def _probe() -> str | None:
    try:
        from fakeprint import _paths

        if not (_paths.profiles_dir() / "generic-wsd.json").is_file():
            return "profiles not found"
        with Server(devices=[Device("p1", "generic-wsd"), Device("p2", "generic-wsd")]) as s:
            if len(s.devices) != 2:
                return "server reported fewer than two devices"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"[:200]
    return None


_WHY = _probe()
needs_multi = pytest.mark.skipif(_WHY is not None, reason=f"no multi-device native/library yet ({_WHY})")


def profiles_two() -> tuple[str, str]:
    from fakeprint import profiles

    ps = profiles()
    a = "generic-wsd" if "generic-wsd" in ps else ps[0]
    b = next((p for p in ps if p != a), a)
    return a, b


@pytest.fixture()
def two():
    a, b = profiles_two()
    with Server(devices=[Device("mono", a), Device("color", b)]) as s:
        yield s


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


@needs_multi
def test_two_devices_have_distinct_identity_and_endpoints(two):
    assert list(two.devices) == ["mono", "color"]
    m, c = two.devices["mono"], two.devices["color"]
    assert m.is_default and not c.is_default and two.default_device is m
    assert len({m.uuid, c.uuid, m.ipp_uuid, c.ipp_uuid}) == 4
    assert all(re.fullmatch(r"[0-9a-f-]{36}", u) for u in (m.uuid, c.uuid))
    assert m.service_endpoint.endswith("/WebServices/PrinterService") and "/d/" not in m.service_endpoint
    assert c.service_endpoint.endswith("/d/color/WebServices/PrinterService")
    assert c.device_endpoint.endswith("/d/color/WebServices/Device")
    assert c.ipp_endpoint.endswith("/d/color/ipp/print") and c.ipp_endpoint.startswith("ipp://127.0.0.1:")
    assert m.friendly_name and c.friendly_name == "fakeprint color"
    assert m.inbox != c.inbox and m.inbox.exists() and c.inbox.exists()
    # srv.* is the default device
    assert two.service_endpoint == m.service_endpoint and two.device_uuid == m.uuid


@needs_multi
def test_derived_uuids_are_stable_and_follow_the_id():
    a, _ = profiles_two()
    with Server(devices=[Device("x", a), Device("y", a)]) as s1, Server(devices=[Device("y", a), Device("x", a)]) as s2:
        assert s1.devices["x"].uuid == s2.devices["x"].uuid and s1.devices["y"].uuid == s2.devices["y"].uuid
        assert s1.devices["x"].uuid != s1.devices["y"].uuid
        assert s1.devices["x"].uuid != "00000000-0000-4000-8000-0000000000f3"


@needs_multi
def test_explicit_uuid_and_name_are_kept():
    a, _ = profiles_two()
    u = "00000000-0000-4000-8000-0000000000a1"
    with Server(devices=[Device("z", a, u, "fakeprint zed")]) as s:
        assert s.devices["z"].uuid == u and s.devices["z"].friendly_name == "fakeprint zed"
        assert s.devices["z"].ipp_uuid != u


@needs_multi
def test_a_job_on_one_device_never_shows_on_the_other(two):
    m, c = two.devices["mono"], two.devices["color"]
    jid = c.printer().print_document(PDF, format="application/pdf")
    wait_for(lambda: c.job(jid) and c.job(jid).state == "Completed")
    assert [j.job_id for j in c.jobs()] == [jid] and m.jobs() == []
    assert c.documents(jid) == [PDF] and m.documents(jid) == []
    assert [x.job_id for x in c.captured()] == [jid] and m.captured() == []
    assert two.jobs() == [] and two.captured() == []  # srv.* is the default device (mono)
    assert c.live()["captured_jobs"] == 1 and m.live()["captured_jobs"] == 0
    assert c.inbox.exists() and any(c.inbox.iterdir()) and not any(m.inbox.iterdir())
    assert c.job_ticket(jid) is not None
    # the same job number on the other device is a different (empty) history
    with pytest.raises(SoapFault):
        m.printer().get_job_status(jid)


@needs_multi
def test_default_device_is_reachable_on_the_legacy_paths(two):
    m = two.devices["mono"]
    jid = two.printer().print_document(PDF, format="application/pdf")
    wait_for(lambda: m.job(jid))
    assert [j.job_id for j in m.jobs()] == [j.job_id for j in two.jobs()] == [jid]
    with httpx.Client() as h:  # raw HTTP, legacy and /d/<id> aliases of the default device
        assert h.get(f"{two.base_url}/v1/status").json()["profile_id"] == m.profile
        assert h.get(f"{two.base_url}/v1/devices").json()[0]["id"] == "mono"


@needs_multi
def test_faults_are_per_device(two):
    m, c = two.devices["mono"], two.devices["color"]
    c.faults(paper_out=True)
    assert c.live()["faults"]["paper_out"] and not m.live()["faults"]["paper_out"]
    assert c.status().printer_state == "Stopped" and m.status().printer_state == "Idle"
    assert m.printer().get_printer_elements().printer_state == "Idle"
    assert c.printer().get_printer_elements().printer_state == "Stopped"
    c.faults(paper_out=False)
    assert c.live()["faults"]["paper_out"] is False
    m.faults(not_accepting_jobs=True)
    assert m.live()["faults"]["offline"] and not c.live()["faults"]["offline"]
    m.clear_printer_faults()


@needs_multi
def test_levels_and_timing_are_per_device_and_never_stale(two):
    m, c = two.devices["mono"], two.devices["color"]
    before = c.refresh()["config"]["published_fingerprint"]
    live = c.set_levels(Black=15)
    assert any(x["name"] == "Black" and x["level"] == 15 for x in live["consumables"])
    assert not any(x["level"] == 15 for x in m.live()["consumables"])
    assert c.refresh()["config"]["published_fingerprint"] == before and not c.needs_readd
    c.set_timing("realistic")
    assert c.live()["timing_preset"] == "realistic" and m.live()["timing_preset"] != "realistic"


@needs_multi
def test_injection_rules_are_per_device(two):
    m, c = two.devices["mono"], two.devices["color"]
    c.inject_fault("GetPrinterElements", reason="boom", count=1)
    assert [r["op"] for r in c.pending_rules()] == ["GetPrinterElements"] and m.pending_rules() == []
    m.printer().get_printer_elements()  # the other printer is unaffected
    with pytest.raises(SoapFault):
        c.printer().get_printer_elements()
    assert c.inject_stats()["hits"] == 1 and m.inject_stats()["hits"] == 0
    c.printer().get_printer_elements()  # the rule was single-shot
    c.inject_delay("GetPrinterElements", 1)
    c.clear_faults()
    assert c.pending_rules() == []


@needs_multi
def test_reset_and_set_profile_touch_only_their_device(two):
    m, c = two.devices["mono"], two.devices["color"]
    m.printer().print_document(PDF, format="application/pdf")
    c.printer().print_document(PDF, format="application/pdf")
    wait_for(lambda: m.jobs() and c.jobs())
    c.reset()
    assert c.jobs() == [] and len(m.jobs()) == 1
    other = next(p for p in two.profiles() if p != m.profile)
    c.set_profile(other)
    assert c.profile == other and c.refresh()["profile"] == other
    assert m.refresh()["profile"] != other or m.profile == other
    with pytest.raises(KeyError):
        c.set_profile("no-such-profile")


@needs_multi
def test_unknown_device_is_404_and_a_device_handle_survives_removal_checks(two):
    status, doc = two.control("GET", "/d/nope/v1/status")
    assert status == 404
    with httpx.Client() as h:
        r = h.get(f"{two.base_url}/d/nope/v1/status")
        assert r.status_code == 404 and r.json().get("error") == "unknown device"
    with pytest.raises(KeyError):
        two._native.reset(device="nope")


@needs_multi
def test_manager_sees_both_devices_and_an_empty_queue(two):
    rows = two.manager.devices()
    assert [r.id for r in rows] == ["mono", "color"] and rows[0].default and not rows[1].default
    assert rows[1].uuid == two.devices["color"].uuid and rows[1].klass
    assert two.manager.queue().empty  # embedded servers never announce
    assert not any(r.announcing for r in rows)


@needs_multi
def test_add_and_remove_a_device_at_run_time(two):
    a, _ = profiles_two()
    late = two.add_device(Device("late", a))
    assert "late" in two.devices and late.uuid and late.base_path == "/d/late"
    jid = late.printer().print_document(PDF, format="application/pdf")
    wait_for(lambda: late.job(jid))
    assert two.devices["mono"].jobs() == []
    with pytest.raises(Conflict):
        two.add_device(Device("late", a))
    with pytest.raises(ApiError):
        two.add_device(Device("bad", "no-such-profile"))
    two.remove_device("late")
    assert "late" not in two.devices
    assert httpx.get(f"{two.base_url}/d/late/v1/status").status_code == 404
    assert [d.id for d in two.manager.devices()] == ["mono", "color"]


@needs_multi
def test_uuid_clash_between_devices_is_rejected_at_start():
    a, _ = profiles_two()
    u = "00000000-0000-4000-8000-0000000000a1"
    with pytest.raises(Exception, match="uuid|UUID"):
        Server(devices=[Device("p", a, u), Device("q", a, u)])


@needs_multi
def test_device_cancel_targets_one_device(two):
    m, c = two.devices["mono"], two.devices["color"]
    c.set_timing("realistic")
    c.set_timing(processing_base_ms=5000)
    jid = c.printer().print_document(PDF, format="application/pdf")
    assert c.device_cancel(jid) is True and m.device_cancel(jid) is False
    wait_for(lambda: c.job(jid) and c.job(jid).state == "Canceled")


@needs_multi
def test_no_listeners_outside_loopback(two):
    assert two.base_url.startswith("http://127.0.0.1:")
    assert all(d.service_endpoint.startswith("http://127.0.0.1:") for d in two.devices.values())
    with pytest.raises(ValueError):
        Server(devices=[Device("a")], host="0.0.0.0")


# ---- the Manager against the library's own manager routes (M2 pieces; NotYet skips) ------------


def _manager_or_skip(srv):
    try:
        srv.manager.registry()
    except NotYet as e:
        pytest.skip(f"manager routes not in this library build: {e}")
    return srv.manager


@needs_multi
def test_install_state_machine_with_the_real_routes(two):
    m = _manager_or_skip(two)
    c = two.devices["color"]
    # a foreign TCPMON port and a port naming two UUIDs can never be recorded as installed
    from fakeprint.manager import BadRequest

    with pytest.raises(BadRequest):
        m.installed("color", {"name": "fakeprint color", "port": "192.168.1.9:9100"})
    with pytest.raises(BadRequest):
        m.installed("color", {"name": "fakeprint color", "port": f"WSD-{c.uuid} WSD-{two.devices['mono'].uuid}"})
    assert m.device("color").state == "new"
    d = m.installed("color", {"name": "fakeprint color", "port": f"WSD-{c.uuid}", "driver": "IPP Class Driver"})
    assert d.state == "installed" and d.install.kind == "wsd" and d.install.windows.port.endswith(c.uuid)
    assert m.registry().find("color").install.state == "installed"
    assert c.refresh()["install"]["state"] == "installed"
    # runtime state never makes an installed printer stale ...
    c.faults(paper_out=True)
    c.set_timing("instant")
    c.clear_printer_faults()
    assert not m.device("color").needs_readd
    # ... a profile change that alters the published capabilities does; switching back heals it
    other = next((p.id for p in m.profiles() if p.id != c.profile and p.id != "generic-wsd"), None)
    if other is None:
        pytest.skip("only one profile available")
    original = c.profile
    after = m.reconfigure("color", other)
    if after.config.published_fingerprint == d.config.published_fingerprint:
        pytest.skip("the two profiles publish identical capabilities")
    assert after.needs_readd and after.state == "stale"
    healed = m.reconfigure("color", original)
    assert healed.state == "installed" and not healed.needs_readd
    # reconcile that no longer lists the printer: stale, offline (not an Add request)
    res = m.reconcile([])
    assert "color" in res.stale
    assert m.device("color").state == "stale" and m.status_table()[1].offline
    assert m.forget("color").state == "new"


@needs_multi
def test_reconcile_with_the_real_daemon_reports_foreign_and_matches_by_uuid(two):
    m = _manager_or_skip(two)
    mono = two.devices["mono"]
    res = m.reconcile([
        {"name": "fakeprint mono (copy)", "port": f"WSD-{mono.uuid}"},
        {"name": "fakeprint mono", "port": "LPT1:"},                 # a name that looks right proves nothing
        {"name": "Both", "port": f"WSD-{mono.uuid} WSD-{two.devices['color'].uuid}"},
    ])
    assert res.installed == ["mono"] and sorted(p.name for p in res.foreign) == ["Both", "fakeprint mono"]
