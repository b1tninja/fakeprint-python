"""Manager over a fake HTTP transport (and a fake named pipe): the JSON contract, typed errors, safety."""

from __future__ import annotations

import json

import httpx
import pytest
from _multi import BASE, FakeDaemon, device_info, uuid_for

from fakeprint import ApiError, Manager
from fakeprint.manager import (
    BadRequest,
    Conflict,
    Forbidden,
    ManagerError,
    NotYet,
    PipeTransport,
    UnknownDevice,
    is_loopback_url,
)

U1 = uuid_for(0xA1)


def make(*devs, **kw):
    fake = FakeDaemon(*devs, **kw)
    return fake, Manager(BASE, http=fake.client())


def test_devices_device_and_registry():
    fake, m = make(device_info("a", default=True), device_info("b", 0xB1))
    assert [d.id for d in m.devices()] == ["a", "b"] == [d.id for d in m.list()]
    assert m.device("b").uuid == uuid_for(0xB1) and m.device("a").default
    with pytest.raises(KeyError):
        m.device("zzz")
    assert [r.name for r in m.registry().devices] == ["a", "b"]


def test_add_posts_the_documented_body_and_returns_a_device():
    fake, m = make()
    d = m.add("mono", "mono-laser", friendly_name="fakeprint mono", jetdirect_port=9101)
    assert fake.calls[0] == ("POST", "/v1/devices", {
        "id": "mono", "profile": "mono-laser", "friendly_name": "fakeprint mono", "jetdirect_port": 9101})
    assert d.id == "mono" and d.friendly_name == "fakeprint mono"
    assert m.add("c", "p", device_uuid=uuid_for(7)).uuid == uuid_for(7)


def test_typed_errors_keep_apierror_compatibility():
    fake, m = make(device_info("a", default=True))
    with pytest.raises(Conflict) as ei:
        m.add("a", "p")
    assert isinstance(ei.value, ManagerError) and isinstance(ei.value, ApiError) and isinstance(ei.value, httpx.HTTPStatusError)
    assert ei.value.status_code == 409 and "duplicate device id" in str(ei.value)
    with pytest.raises(BadRequest):
        m.add("", "p")
    with pytest.raises(UnknownDevice) as ui_:
        m.forget("nope")
    assert isinstance(ui_.value, KeyError) and "unknown device" in str(ui_.value) and ui_.value.status_code == 404
    _, m2 = make(loopback=False)
    with pytest.raises(Forbidden):
        m2.add("a", "p")


def test_501_and_missing_routes_are_not_yet():
    def h(req):
        return httpx.Response(501, json={"error": "not implemented yet: add (planned for WP-M step M1)"})

    m = Manager(BASE, http=httpx.Client(base_url=BASE, transport=httpx.MockTransport(h)))
    with pytest.raises(NotYet) as e:
        m.add("a", "p")
    assert e.value.status_code == 501 and "not implemented yet" in str(e.value)
    _, old = make(device_info("a", default=True), missing=("/v1/queue", "/v1/registry", "/v1/devices/"))
    with pytest.raises(NotYet):
        old.queue()
    with pytest.raises(NotYet):
        old.registry()
    with pytest.raises(NotYet):
        old.remove("a")
    assert [d.id for d in old.devices()] == ["a"]  # the M0 route still works


def test_remove_forget_flag_and_reconfigure_forget():
    fake, m = make(device_info("a", default=True), device_info("b", 0xB1))
    m.remove("b", forget=True)
    m.remove("a")
    assert ("DELETE", "/v1/devices/b?forget=true", None) in fake.calls and ("DELETE", "/v1/devices/a", None) in fake.calls
    assert fake.devices == []
    fake, m = make(device_info("a", default=True))
    assert m.reconfigure("a", "color-inkjet-duplex").profile == "color-inkjet-duplex"
    assert fake.calls[-1] == ("POST", "/v1/devices/a/reconfigure", {"profile": "color-inkjet-duplex"})
    assert m.forget("a").state == "new"


def test_installed_is_checked_locally_before_anything_is_sent():
    fake, m = make(device_info("a", 0xA1, default=True))
    with pytest.raises(BadRequest):
        m.installed("a", {"name": "fakeprint a", "port": "192.168.1.5:9100"})
    with pytest.raises(BadRequest):
        m.installed("a", {"name": "fakeprint a", "port": f"WSD-{U1} WSD-{uuid_for(0xB1)}"})
    assert not any(p.endswith("/installed") for _, p, _ in fake.calls)
    d = m.installed("a", {"name": "fakeprint a", "port": f"WSD-{U1}", "driver": "IPP Class Driver"})
    assert d.state == "installed" and d.install.kind == "wsd"
    sent = [b for _, p, b in fake.calls if p.endswith("/installed")][0]
    assert sent == {"windows": {"name": "fakeprint a", "port": f"WSD-{U1}", "driver": "IPP Class Driver", "id": ""}}
    # without the local check the daemon is still the authority
    with pytest.raises(BadRequest):
        m.installed("a", {"name": "x", "port": "LPT1:"}, check=False)
    with pytest.raises(UnknownDevice):
        m.installed("zzz", {"name": "x", "port": f"WSD-{U1}"})


def test_reconcile_withholds_ambiguous_ports_and_reports_foreign():
    fake, m = make(device_info("a", 0xA1, default=True), device_info("b", 0xB1))
    res = m.reconcile([
        {"name": "A", "port": f"WSD-{U1}"},
        {"name": "Both", "port": f"WSD-{U1} WSD-{uuid_for(0xB1)}"},
        {"name": "Real", "port": "10.0.0.2:9100"},
    ])
    sent = [b for _, p, b in fake.calls if p == "/v1/reconcile"][0]
    assert [p["name"] for p in sent["printers"]] == ["A", "Real"]  # the two-UUID row never leaves
    assert res.installed == ["a"] and sorted(p.name for p in res.foreign) == ["Both", "Real"]
    assert m.device("a").state == "installed" and m.device("b").state == "new"
    # the next reconcile no longer lists A: stale (offline)
    assert m.reconcile([]).stale == ["a"]


def test_reconcile_from_windows_uses_the_given_lister_read_only():
    fake, m = make(device_info("a", 0xA1, default=True))

    class PI:  # winprint.PrinterInfo-like
        name, port, driver = "fakeprint a", f"WSD-{U1}", "d"

    assert m.reconcile_from_windows(lambda: [PI()]).installed == ["a"]


def test_reconcile_from_windows_without_winprint_says_how_to_proceed(monkeypatch):
    import sys

    import fakeprint

    _, m = make()
    monkeypatch.delattr(fakeprint, "winprint", raising=False)
    monkeypatch.setitem(sys.modules, "fakeprint.winprint", None)  # as on a platform without ctypes/winspool
    with pytest.raises(RuntimeError, match="list_printers"):
        m.reconcile_from_windows()


def test_queue_advance_skip_reorder_and_guidance():
    fake, m = make(device_info("a", 0xA1, default=True), device_info("b", 0xB1), device_info("c", 0xC1))
    q = m.queue()
    assert (q.head, q.remaining, q.position, q.total, q.settle_s, q.head_timeout_s) == ("a", ["b", "c"], 1, 3, 5, 180)
    assert q.ids == ["a", "b", "c"]
    assert m.guidance().startswith("Add now: fakeprint a (1 of 3).")
    assert m.skip().head == "b" and m.reorder(["c", "a", "b"]).head == "c"
    assert fake.calls[-1] == ("POST", "/v1/queue", {"order": ["c", "a", "b"]})
    assert m.advance().total == 3
    _, empty = make(device_info("a", default=True, install={"state": "installed"}))
    assert empty.queue().empty and empty.guidance() is None


def test_levels_target_the_device_path_and_return_live():
    fake, m = make(device_info("mono", 0xA1))
    live = m.levels("mono", {"Black": 15}, {"Tray1": 0})
    assert fake.calls[-1] == ("POST", "/d/mono/v1/levels", {"consumables": {"Black": 15}, "inputs": {"Tray1": 0}})
    assert live["consumables"][0]["level"] == 15


def test_profiles_typed_with_local_fallback(tmp_path, monkeypatch):
    fake, m = make()
    assert [p.id for p in m.profiles()] == ["mono-laser", "color-inkjet-duplex"]
    (tmp_path / "local-one.json").write_text(json.dumps({"label": "L", "formats": ["x"]}), encoding="utf-8")
    monkeypatch.setenv("FAKEPRINT_PROFILES", str(tmp_path))
    _, old = make(missing=("/v1/profiles",))
    ps = old.profiles()
    assert [(p.id, p.source) for p in ps] == [("local-one", "files")]


def test_open_settings_asks_the_daemon_and_never_raises():
    fake, m = make()
    assert m.open_settings() is True and fake.opened_settings == 1
    assert m.open_add_device_settings(opener=lambda u: pytest.fail("must not fall back")) == "daemon"
    _, old = make(missing=("/v1/ctl",))
    opened = []
    assert old.open_settings() is False
    assert old.open_add_device_settings(opener=opened.append) == "startfile" and opened == ["ms-settings:printers"]
    remote = Manager("http://192.0.2.7:3921", http=httpx.Client(base_url="http://192.0.2.7:3921", transport=FakeDaemon().transport))
    assert remote.open_settings() is False  # never asks a non-loopback daemon


def test_open_ui_uses_the_injected_opener():
    _, m = make()
    seen = []
    assert m.open_ui(seen.append) == f"{BASE}/ui" == m.ui_url and seen == [f"{BASE}/ui"]


def test_wait_installed_is_bounded_and_polls_without_adding():
    fake, m = make(device_info("a", 0xA1, default=True))
    t = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s
        if len(sleeps) == 3:  # a human finishes the Add
            fake.devices[0]["install"] = {"state": "installed"}

    assert m.wait_installed("a", timeout=10, interval=1, sleep=sleep, clock=lambda: t[0]).state == "installed"
    assert len(sleeps) == 3
    fake.devices[0]["install"] = {"state": "new"}
    with pytest.raises(TimeoutError):
        m.wait_installed("a", timeout=2, interval=1, sleep=sleep, clock=lambda: t[0])
    assert all(c[0] == "GET" for c in fake.calls[-4:]) and not any("Add-Printer" in str(c) for c in fake.calls)


def test_status_table_summary_level_and_unreachable_is_red():
    fake, m = make(device_info("a", 0xA1, default=True, install={"state": "installed"}), device_info("b", 0xB1))
    rows = m.status_table()
    assert [(r.id, r.install_state) for r in rows] == [("a", "installed"), ("b", "new")]
    assert m.summary_level() == "amber"

    def boom(req):
        raise httpx.ConnectError("refused")

    down = Manager(BASE, http=httpx.Client(base_url=BASE, transport=httpx.MockTransport(boom)))
    assert down.summary_level() == "red"


def test_is_loopback_url():
    assert is_loopback_url("http://127.0.0.1:3921") and is_loopback_url("http://localhost:1") and is_loopback_url("http://[::1]:2")
    assert not is_loopback_url("http://192.168.1.2:3921") and not is_loopback_url("http://evil.example")


# ---- named pipe ------------------------------------------------------------------------------


class FakePipe:
    """Opener for PipeTransport: records the request line and answers from a script."""

    def __init__(self, answer):
        self.answer, self.requests, self.paths = answer, [], []

    def __call__(self, path):
        self.paths.append(path)
        outer = self

        class F:
            def write(self, b):
                outer.requests.append(json.loads(b.decode()))

            def flush(self):
                pass

            def readline(self):
                req = outer.requests[-1]
                return (json.dumps(outer.answer(req)) + "\n").encode()

            def close(self):
                pass

        return F()


def pipe_manager(answer):
    opener = FakePipe(answer)
    return Manager.pipe("lab", opener=opener), opener


def test_pipe_path_and_ctl_commands():
    m, op = pipe_manager(lambda r: {"ok": True, "result": [device_info("a", default=True)] if r["cmd"] == "devices" else {"opened": True}})
    assert m.devices()[0].id == "a" and op.paths == [r"\\.\pipe\fakeprint-lab"] and op.requests[0] == {"cmd": "devices"}
    assert m.open_settings() is True and op.requests[-1] == {"cmd": "open_settings", "args": {"page": "printers"}}
    assert repr(m).endswith(r"fakeprint-lab)")


def test_pipe_requests_use_the_documented_ctl_names():
    seen = []

    def answer(req):
        seen.append(req)
        if req["cmd"] == "queue":
            return {"ok": True, "result": {"head": "a", "remaining": [], "position": 1, "total": 1}}
        return {"ok": True, "result": device_info("a", default=True)}

    m, _ = pipe_manager(answer)
    m.add("a", "p", device_uuid=U1)
    m.remove("a", forget=True)
    m.reconfigure("a", "q")
    m.forget("a")
    m.queue()
    m.skip()
    m.advance()
    m.reorder(["a"])
    m.levels("a", {"Black": 1})
    m.mark_added("a")
    assert [(r["cmd"], r.get("args")) for r in seen] == [
        ("device_add", {"id": "a", "profile": "p", "device_uuid": U1}),
        ("device_remove", {"device": "a", "forget": True}),
        ("reconfigure", {"device": "a", "profile": "q"}),
        ("forget", {"device": "a"}),
        ("queue", None), ("queue_skip", None), ("queue_advance", None),
        ("queue_reorder", {"order": ["a"]}),
        ("levels", {"device": "a", "consumables": {"Black": 1}}),
        ("mark_added", {"device": "a"}),
    ]


@pytest.mark.parametrize(
    "error,cls",
    [
        ("not implemented yet: add (planned for WP-M step M1)", NotYet),
        ("unknown device", UnknownDevice),
        ("duplicate device id 'a'", Conflict),
        ("loopback peers only", Forbidden),
        ("bad profile", BadRequest),
    ],
)
def test_pipe_errors_map_to_the_same_types(error, cls):
    m, _ = pipe_manager(lambda r: {"ok": False, "error": error})
    with pytest.raises(cls):
        m.add("a", "p")


def test_pipe_has_no_url_to_open_and_empty_answer_is_an_oserror():
    m, _ = pipe_manager(lambda r: {"ok": True, "result": {}})
    with pytest.raises(ValueError):
        m.open_ui()
    p = PipeTransport("x", opener=lambda path: type("F", (), {"write": lambda s, b: 0, "readline": lambda s: b"", "close": lambda s: None})())
    with pytest.raises(OSError):
        p.ctl("devices")
