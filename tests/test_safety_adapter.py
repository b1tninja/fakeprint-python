"""fakeprint.safety: the adapter over fakescan.safety. Recording stubs only: no spooler listing, no LAN."""

from __future__ import annotations

import sys
import types
from collections import namedtuple
from types import SimpleNamespace

import pytest

pytest.importorskip("fakescan.safety", reason="fakescan.safety (the shared device guard) not on the path")

from fakeprint import safety  # noqa: E402

FAKE = "00000000-0000-4000-8000-00000000abcd"
REAL = "e3248000-80ce-11db-8000-3c2af435905f"


@pytest.fixture(autouse=True)
def _policy():
    old = safety.set_policy(safety.Policy(queue_resolver=None))
    yield
    safety.set_policy(old)


def test_reexports_the_shared_implementation():
    import fakescan.safety as shared

    assert safety.Policy is shared.Policy and safety.DeviceNotAllowed is shared.DeviceNotAllowed
    assert safety.FAKE_UUID_PREFIX == "00000000-0000-4000-8000-"
    import fakeprint

    assert fakeprint.FAKE_UUID_PREFIX == safety.FAKE_UUID_PREFIX  # one convention in both packages


def test_device_ref_from_objects_and_rows():
    dev = SimpleNamespace(uuid=FAKE, service_endpoint="http://127.0.0.1:5357/p", device_endpoint=None, friendly_name="fakeprint x")
    ref = safety.device_ref(dev)
    assert ref == {"uuid": FAKE, "service_endpoint": "http://127.0.0.1:5357/p", "name": "fakeprint x"}
    assert safety.allowed(ref)
    srv = SimpleNamespace(service_endpoint="http://127.0.0.1:1/p", default_device=SimpleNamespace(uuid=FAKE))
    assert safety.device_ref(srv)["uuid"] == FAKE
    row = {"id": "inkjet1", "uuid": REAL, "name": "Office"}
    assert not safety.allowed(safety.device_ref(row))
    assert safety.device_ref({}) == {} and not safety.allowed(safety.device_ref({}))  # unprovable: excluded


def test_guarded_print_client_checks_before_constructing(monkeypatch):
    import fakeprint

    made = []
    monkeypatch.setattr(fakeprint, "PrintClient", lambda ep, **kw: made.append(ep) or "client")
    assert safety.guarded_print_client("http://127.0.0.1:5357/p") == "client"
    with pytest.raises(safety.DeviceNotAllowed):
        safety.guarded_print_client("http://192.0.2.1:5357/p")
    assert made == ["http://127.0.0.1:5357/p"]


def test_install_guards_printclient_and_winprint_without_touching_the_spooler(monkeypatch):
    import fakeprint.soap as soap

    PI = namedtuple("PI", "name port")
    calls = []
    fake_wp = types.ModuleType("fakeprint.winprint")
    fake_wp.list_printers = lambda: [PI("fake q", "WSD-1"), PI("Office", "WSD-2")]
    fake_wp.wsd_port_uuid = {"WSD-1": FAKE, "WSD-2": REAL}.get
    for n in ("print_raw", "print_image", "job_status", "wait_for_job"):
        setattr(fake_wp, n, (lambda name: lambda printer, *a, **k: calls.append((name, printer)))(n))
    for f in ("list_printers", "wsd_port_uuid", "print_raw", "print_image", "job_status", "wait_for_job"):
        getattr(fake_wp, f).__module__ = "fakeprint.winprint"
    monkeypatch.setitem(sys.modules, "fakeprint.winprint", fake_wp)
    orig_init = soap.PrintClient.__init__
    inst = safety.install(modules=["fakeprint.soap", "fakeprint.winprint"])
    try:
        assert "fakeprint.soap.PrintClient.__init__" in inst.patched
        with pytest.raises(safety.DeviceNotAllowed):
            soap.PrintClient("http://192.0.2.1:5357/p")
        c = soap.PrintClient("http://127.0.0.1:5357/p")
        c.close()
        assert [q.name for q in fake_wp.list_printers()] == ["fake q"]
        fake_wp.print_image("fake q")
        with pytest.raises(safety.DeviceNotAllowed):
            fake_wp.print_image("Office")
        with pytest.raises(safety.DeviceNotAllowed):
            fake_wp.print_raw("not installed")
        assert calls == [("print_image", "fake q")]
    finally:
        inst.uninstall()
    assert soap.PrintClient.__init__ is orig_init


@pytest.mark.embedded
def test_embedded_server_is_provably_fake():
    pytest.importorskip("fakeprint._native", reason="native module not built")
    from fakeprint.server import Server

    s = Server()
    try:
        ref = safety.device_ref(s)
        assert ref.get("uuid", "").startswith(safety.FAKE_UUID_PREFIX), "the embedded device must be in the fake namespace"
        assert safety.allowed(ref) and safety.allowed(s.service_endpoint)
        safety.guarded_print_client(s.service_endpoint).close()
    finally:
        s.stop()
