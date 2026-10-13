"""Device/Server multi-device API without a multi-device native module.

* the single-device Server keeps its API and now exposes ``devices`` / ``manager`` (real native, today's library);
* Device on a stub server (fake HTTP) covers the non-default paths, so the logic is tested before the Rust lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from _multi import BASE, FakeDaemon, device_info, uuid_for

from fakeprint import Device, Manager, Server
from fakeprint.client import Client


# ---- Device as a description -------------------------------------------------------------------


def test_device_description_validation_and_spec():
    d = Device("mono", profile="mono-laser")
    assert d.spec() == ("mono", "mono-laser", None, None) and not d.bound and d.uuid is None
    assert Device("x", "p", uuid_for(3), "fakeprint x").spec() == ("x", "p", uuid_for(3), "fakeprint x")
    for bad in ("", "a b", "x" * 33, "a/b", "ü"):
        with pytest.raises(ValueError):
            Device(bad)
    with pytest.raises(ValueError):
        Device("ok", device_uuid="not-a-uuid")
    with pytest.raises(RuntimeError, match="not running"):
        d.printer()
    assert repr(d) == "Device('mono', profile='mono-laser')"


def test_server_rejects_conflicting_configuration():
    with pytest.raises(ValueError, match="implicit default device"):
        Server(devices=[Device("a")], device_uuid=uuid_for(1))
    with pytest.raises(ValueError, match="implicit default device"):
        Server(devices=[Device("a")], name="x")
    with pytest.raises(ValueError, match="duplicate device ids"):
        Server(devices=[Device("a"), Device("a", "p")])


# ---- the single-device Server on the real native module ----------------------------------------


@pytest.mark.embedded
def test_single_device_server_has_one_default_device(srv):
    assert list(srv.devices) == ["default"]
    d = srv.devices["default"]
    assert d.is_default and d.base_path == "" and d.id == "default"
    assert d.uuid == srv.device_uuid and d.ipp_uuid == srv.ipp_uuid
    assert d.service_endpoint == srv.service_endpoint and d.device_endpoint == srv.device_endpoint
    assert d.profile == srv.status().profile_id and Path(d.inbox) == srv.inbox_dir
    assert srv.default_device is d


@pytest.mark.embedded
def test_default_device_api_matches_the_server_api(srv):
    d = srv.devices["default"]
    job = d.printer().print_document(b"%PDF-1.4 default", format="application/pdf")
    assert [j.job_id for j in d.jobs()] == [j.job_id for j in srv.jobs()] == [job]
    assert d.documents(job) == srv.documents(job) == [b"%PDF-1.4 default"]
    assert [c.job_id for c in d.captured()] == [job] and d.job(job).state == srv.job(job).state
    assert d.job_ticket(job) is not None
    d.faults(paper_out=True)
    assert srv.faults()["active"]["printer_state"] == "Stopped"
    d.clear_faults()
    assert srv.faults()["active"]["printer_state"] == "Idle"
    assert d.status().profile_id == srv.status().profile_id
    d.reset()
    assert d.jobs() == [] and d.validation_problem_count() == 0


@pytest.mark.embedded
def test_default_device_clients_and_manager_binding(srv):
    d = srv.devices["default"]
    c = d.client()
    assert isinstance(c, Client) and c.device is None and c.status().profile_id == srv.status().profile_id
    assert isinstance(srv.manager, Manager) and srv.manager is srv.manager and srv.manager.base_url == srv.base_url
    seen = []
    assert srv.open_ui(seen.append) == f"{srv.base_url}/ui" and seen == [f"{srv.base_url}/ui"]


@pytest.mark.embedded
def test_server_without_devices_argument_is_unchanged(srv):
    # the constructor, properties and methods used before multi-device still exist and agree
    assert srv.port > 0 and srv.base_url.endswith(str(srv.port)) and srv.running
    assert isinstance(srv.status().profile_id, str) and srv.profiles()
    assert srv.control("GET", "/health")[0] == 200


# ---- Device on a stub server (non-default paths over fake HTTP) --------------------------------


class StubServer:
    """Just enough of Server for Device: the default id, device infos and a fake-HTTP control plane."""

    def __init__(self, fake: FakeDaemon, default: str = "a") -> None:
        self._default_id = default
        self.base_url = BASE
        self.fake = fake
        self._clients: list = []
        self.cancels: list = []

    def _device_infos(self):
        return {d["id"]: dict(d) for d in self.fake.devices}

    def _device_control(self, dev, method, path, body):
        r = self.fake.client().request(method, dev.base_path + path, json=body)
        return r.status_code, (r.json() if r.content else None)

    def http(self, **kw):
        return httpx.Client(**kw)

    def _device_cancel(self, dev, job_id):
        self.cancels.append((dev.id, job_id))
        return True


def bound(fake, id, default="a"):
    srv = StubServer(fake, default)
    info = next(d for d in fake.devices if d["id"] == id)
    return Device(id)._bound_to(srv, info), srv


def test_bound_device_identity_and_paths():
    fake = FakeDaemon(device_info("a", 0xA1, default=True), device_info("b", 0xB1, profile="color-inkjet-duplex"))
    a, _ = bound(fake, "a")
    b, srv = bound(fake, "b")
    assert (a.base_path, b.base_path) == ("", "/d/b") and a.is_default and not b.is_default
    assert b.uuid == uuid_for(0xB1) and b.ipp_uuid == uuid_for(0xB2) and b.profile == "color-inkjet-duplex"
    assert b.friendly_name == "fakeprint b" and b.inbox == Path("C:\\inbox\\b") == b.inbox_dir
    assert b.service_endpoint == f"{BASE}/d/b/WebServices/PrinterService" and b.ipp_endpoint == "ipp://127.0.0.1:3921/d/b/ipp/print"
    assert b.device_endpoint.endswith("/d/b/WebServices/Device") and b.class_ == "3fa9c2d1e07b" and not b.needs_readd
    assert "uuid=" + uuid_for(0xB1) in repr(b)
    c = b.client()
    assert c.device == "b" and c.prefix == "/d/b" and a.client().prefix == ""


def test_bound_device_control_plane_goes_to_its_prefix():
    fake = FakeDaemon(device_info("a", 0xA1, default=True), device_info("b", 0xB1))
    b, srv = bound(fake, "b")
    live = b.set_levels({"Black": 15}, inputs={"Tray1": 0})
    assert fake.calls[-1] == ("POST", "/d/b/v1/levels", {"consumables": {"Black": 15}, "inputs": {"Tray1": 0}})
    assert live["consumables"][0]["level"] == 15
    with pytest.raises(ValueError, match="needs consumables"):
        b.set_levels()
    assert b.device_cancel(5) is True and srv.cancels == [("b", 5)]


def test_client_scoping_only_prefixes_the_control_plane():
    seen: list[str] = []

    def h(req):
        seen.append(req.url.path)
        if req.url.path == "/health":
            return httpx.Response(200, text="ok")
        return httpx.Response(200, json={"id": 3, "state": "Completed"} if "/jobs/" in req.url.path else {})

    c = Client(BASE, device="mono", http=httpx.Client(base_url=BASE, transport=httpx.MockTransport(h)))
    c.health()
    c.status()
    c.print_jobs()
    c.job(3)
    c.set_faults(paper_out=True)
    c.timing(preset="instant")
    c.levels({"Black": 5})
    c.devices()
    c.ctl("faults", paper_out=True)
    assert seen == [
        "/health", "/d/mono/v1/status", "/d/mono/v1/jobs", "/d/mono/v1/jobs/3", "/d/mono/v1/faults",
        "/d/mono/v1/timing", "/d/mono/v1/levels", "/v1/devices", "/d/mono/v1/ctl",
    ]


def test_scoped_ctl_carries_the_device_argument():
    bodies = []

    def h(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={})

    c = Client(BASE, device="mono", http=httpx.Client(base_url=BASE, transport=httpx.MockTransport(h)))
    c.ctl("faults", paper_out=True)
    c.ctl("reset", device="other")
    assert bodies == [{"cmd": "faults", "args": {"paper_out": True, "device": "mono"}},
                      {"cmd": "reset", "args": {"device": "other"}}]


def test_status_exposes_device_id():
    from fakeprint.client import Status

    assert Status({"device_id": "mono"}).device_id == "mono" and Status({}).device_id == ""
