"""lab, tray (ensure_daemon), ctl and the MCP tools against the fake daemon: nothing is ever Added."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _multi import BASE, FakeDaemon, device_info, uuid_for

from fakeprint import ctl, lab, mcp_server, tray
from fakeprint.manager import Manager
from fakeprint.mcp_server import GuardError, Tools

U1 = uuid_for(0xA1)


def make(*devs, **kw):
    fake = FakeDaemon(*devs, **kw)
    return fake, Manager(BASE, http=fake.client())


# ---- lab -------------------------------------------------------------------------------------


def test_spec_parse_forms():
    assert lab.Spec.parse("mono=mono-laser") == lab.Spec("mono", "mono-laser")
    assert lab.Spec.parse("mono=mono-laser@" + U1) == lab.Spec("mono", "mono-laser", U1)
    assert lab.Spec.parse("generic-wsd") == lab.Spec("generic-wsd", "generic-wsd")


def test_up_registers_printers_and_names_only_the_first_to_add():
    fake, m = make()
    rep = lab.up(m, ["mono=mono-laser", {"id": "color", "profile": "color-inkjet-duplex"}, lab.Spec("c3", "p")])
    assert rep.added == ["mono", "color", "c3"] and not rep.existing and not rep.failed
    assert rep.instruction.device_id == "mono" and (rep.instruction.position, rep.instruction.total) == (1, 3)
    assert rep.instruction.text.startswith('Add now: fakeprint mono (1 of 3). Open Settings')
    assert "Nothing" not in rep.text() and rep.text().count("Add now:") == 1  # ONE printer at a time
    # a second `up` keeps what exists and registers nothing twice
    again = lab.up(m, ["mono=mono-laser", "extra=x"])
    assert again.existing == ["mono"] and again.added == ["extra"]
    # never anything but registry calls
    assert {c[1] for c in fake.calls if c[0] == "POST"} <= {"/v1/devices"}


def test_up_reports_failures_per_printer_and_supports_all_profiles_with_limit():
    fake, m = make(device_info("mono-laser", default=True, install={"state": "installed"}))
    rep = lab.up(m, [], all_profiles=True, limit=5)
    assert rep.existing == ["mono-laser"] and rep.added == ["color-inkjet-duplex"]
    bad = lab.up(m, [lab.Spec("", "p")])
    assert "" in bad.failed and "id and profile required" in bad.failed[""]
    assert lab.up(m, [], all_profiles=True, limit=0).added == []


def test_up_with_nothing_to_add_says_so():
    _, m = make(device_info("a", default=True, install={"state": "installed"}))
    rep = lab.up(m, ["a=mono-laser"])
    assert rep.instruction is None and "Nothing to Add in Windows." in rep.text()


def test_down_never_removes_installed_printers_unless_asked():
    fake, m = make(device_info("inst", 0xA1, default=True, install={"state": "installed"}), device_info("new", 0xB1))
    rep = lab.down(m)
    assert rep.removed == ["new"] and not rep.kept_installed
    rep = lab.down(m, ["inst", "ghost"])
    assert rep.kept_installed == ["inst"] and rep.missing == ["ghost"] and [d["id"] for d in fake.devices] == ["inst"]
    assert lab.down(m, ["inst"], include_installed=True, forget=True).removed == ["inst"]
    assert ("DELETE", "/v1/devices/inst?forget=true", None) in fake.calls


def test_wait_walks_the_queue_one_printer_at_a_time_and_is_bounded():
    fake, m = make(device_info("a", 0xA1, default=True), device_info("b", 0xB1), device_info("c", 0xC1))
    t = [0.0]
    out: list[str] = []
    sleeps = {"n": 0}

    def sleep(s):
        t[0] += s
        sleeps["n"] += 1
        head = m.queue().head
        if sleeps["n"] % 2 == 0 and head in ("a", "b"):  # the human finishes the head
            fake._find(head)["install"] = {"state": "installed"}

    rep = lab.wait(m, per_device_timeout=50, interval=1, out=out.append, sleep=sleep, clock=lambda: t[0])
    assert rep.installed == ["a", "b"] and rep.timed_out == ["c"]  # c is never clicked: bounded, stops there
    assert [i.device_id for i in rep.shown] == ["a", "b", "c"]
    assert out[0] == "Add 1 of 3: a now" and "Add 1 of 2: b now" in out and out.count("Add 1 of 1: c now") == 1
    assert sum(1 for l in out if l.startswith("Add now:")) == 3
    assert not any(c[0] == "POST" and c[1] != "/v1/reconcile" for c in fake.calls)  # it only reads


def test_wait_calls_the_reconcile_hook_and_survives_its_failure():
    fake, m = make(device_info("a", 0xA1, default=True))
    calls = []
    t = [0.0]

    def reconcile():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("spooler busy")
        fake.devices[0]["install"] = {"state": "installed"}

    out: list[str] = []
    rep = lab.wait(m, per_device_timeout=30, interval=1, out=out.append, reconcile=reconcile,
                   sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert rep.installed == ["a"] and len(calls) == 2 and any("reconcile failed: spooler busy" in l for l in out)


def test_ensure_attaches_without_spawning_and_never_adds():
    spawned = []
    m = lab.ensure(BASE, opener=lambda *a, **k: spawned.append(a), health_check=lambda u: True)
    assert isinstance(m, Manager) and spawned == []
    m.close()


def test_lab_cli_up_status_down(monkeypatch, capsys):
    fake = FakeDaemon()
    monkeypatch.setattr(lab, "Manager", lambda url: Manager(url, http=fake.client()))
    assert lab.main(["up", "mono=mono-laser", "color=color-inkjet-duplex"]) == 0
    out = capsys.readouterr().out
    assert "registered: mono, color" in out and "Add now: fakeprint mono (1 of 2)" in out
    assert lab.main(["queue"]) == 0 and "head=mono" in capsys.readouterr().out
    assert lab.main(["guidance"]) == 0 and "Add now:" in capsys.readouterr().out
    assert lab.main(["status"]) == 0 and "level: amber" in capsys.readouterr().out
    assert lab.main(["down", "mono"]) == 0 and "removed: mono" in capsys.readouterr().out
    assert lab.main(["open-settings"]) == 0 and fake.opened_settings == 1


def test_lab_cli_reports_manager_errors_with_exit_1(monkeypatch, capsys):
    fake = FakeDaemon(missing=("/v1/queue",))
    monkeypatch.setattr(lab, "Manager", lambda url: Manager(url, http=fake.client()))
    assert lab.main(["queue"]) == 1 and "fakeprint-lab:" in capsys.readouterr().err


# ---- tray ------------------------------------------------------------------------------------


def test_ensure_daemon_attach_first():
    assert tray.ensure_daemon(BASE, opener=lambda *a, **k: pytest.fail("spawned"), health_check=lambda u: True) == "attached"


def test_ensure_daemon_spawns_manage_with_a_stub_and_waits_for_health(tmp_path):
    exe = tmp_path / "fakeprint.exe"
    exe.write_bytes(b"")
    state = {"spawned": None, "polls": 0}

    def opener(cmd, **kw):
        state["spawned"] = (cmd, kw)

    def health(url):
        state["polls"] += 1
        return state["spawned"] is not None and state["polls"] > 2

    got = tray.ensure_daemon("http://127.0.0.1:3923", exe, ["--no-tray"], opener, health_check=health, sleep=lambda s: None)
    cmd, kw = state["spawned"]
    assert got == "started" and cmd == [str(exe), "--manage", "--no-tray", "--listen", "127.0.0.1:3923"]
    assert kw["stdin"] is not None and "creationflags" in kw or "start_new_session" in kw
    # default port: no --listen; explicit --listen is never duplicated
    state.update(spawned=None, polls=0)
    tray.ensure_daemon(BASE, exe, (), opener, health_check=health, sleep=lambda s: None)
    assert state["spawned"][0] == [str(exe), "--manage"]


def test_ensure_daemon_refuses_remote_and_reports_missing_exe_and_timeout(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        tray.ensure_daemon("http://192.0.2.9:3921", health_check=lambda u: False)
    with pytest.raises(FileNotFoundError):
        tray.ensure_daemon(BASE, tmp_path / "nope.exe", health_check=lambda u: False)
    monkeypatch.setattr(tray, "find_daemon_exe", lambda exe=None: None)
    with pytest.raises(FileNotFoundError):
        tray.ensure_daemon(BASE, health_check=lambda u: False)
    exe = tmp_path / "fakeprint.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(tray, "find_daemon_exe", lambda e=None: exe)
    t = iter(range(0, 1000))
    monkeypatch.setattr(tray.time, "monotonic", lambda: next(t))
    with pytest.raises(TimeoutError):
        tray.ensure_daemon(BASE, opener=lambda *a, **k: None, health_check=lambda u: False, timeout=3, sleep=lambda s: None)


def test_find_daemon_exe_order(tmp_path, monkeypatch):
    exe = tmp_path / ("fakeprint.exe" if tray.os.name == "nt" else "fakeprint")
    exe.write_bytes(b"")
    monkeypatch.setenv("FAKEPRINT_EXE", str(exe))
    assert tray.find_daemon_exe() == exe
    assert tray.find_daemon_exe(exe) == exe
    monkeypatch.delenv("FAKEPRINT_EXE")
    monkeypatch.setattr(tray.shutil, "which", lambda n: None)
    monkeypatch.setattr(tray, "KNOWN_DAEMON_DIR", tmp_path / "none")
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "t"))
    (tmp_path / "t" / "release").mkdir(parents=True)
    (tmp_path / "t" / "release" / exe.name).write_bytes(b"")
    assert tray.find_daemon_exe() == tmp_path / "t" / "release" / exe.name


# ---- ctl -------------------------------------------------------------------------------------


def test_ctl_cli(monkeypatch, capsys):
    fake = FakeDaemon(device_info("a", 0xA1, default=True), device_info("b", 0xB1))
    monkeypatch.setattr(ctl, "Manager", lambda url=BASE, **k: Manager(url, http=fake.client()))
    assert ctl.main(["status", "--width", "120"]) == 0
    out = capsys.readouterr().out
    assert "a" in out and "level: amber" in out
    assert ctl.main(["add", "z", "--profile", "mono-laser", "--name", "fakeprint z"]) == 0
    assert "added z" in capsys.readouterr().out
    assert ctl.main(["add", "z", "--profile", "mono-laser"]) == 1 and "duplicate" in capsys.readouterr().err
    assert ctl.main(["queue", "--skip"]) == 0 and "head=b" in capsys.readouterr().out
    assert ctl.main(["levels", "a", "--consumable", "Black=10"]) == 0
    assert ctl.main(["faults", "a", "--paper-out", "true", "--offline", "false"]) == 0
    sent = [c for c in fake.calls if c[1] == "/v1/ctl"][-1][2]
    assert sent == {"cmd": "faults", "args": {"device": "a", "paper_out": True, "not_accepting_jobs": False}}
    assert ctl.main(["faults", "a"]) == 2
    assert ctl.main(["remove", "z", "--forget"]) == 0
    assert ctl.main(["profiles"]) == 0 and "mono-laser" in capsys.readouterr().out
    assert ctl.main(["open-settings"]) == 0


# ---- MCP -------------------------------------------------------------------------------------


def tools(*devs, **kw):
    fake = FakeDaemon(*devs, **kw)
    return fake, Tools(BASE, http=fake.client())


def test_mcp_guards_refuse_remote_and_dead_daemons():
    with pytest.raises(GuardError):
        Tools("http://192.0.2.7:3921")
    Tools("http://192.0.2.7:3921", allow_remote=True)
    import httpx

    def boom(req):
        raise httpx.ConnectError("refused")

    dead = Tools(BASE, http=httpx.Client(base_url=BASE, transport=httpx.MockTransport(boom)))
    assert dead.health()["ok"] is False and "fakeprint-lab ensure" in dead.health()["error"]
    with pytest.raises(GuardError):
        dead.devices()


def test_mcp_read_tools():
    fake, t = tools(device_info("a", 0xA1, default=True, install={"state": "installed"}), device_info("b", 0xB1))
    assert t.health()["ok"] is True
    ds = t.devices()
    assert [d["id"] for d in ds] == ["a", "b"] and ds[0]["class"] == "3fa9c2d1e07b" and ds[1]["state"] == "new"
    g = t.queue_guidance()
    assert g["head"] == "b" and g["guidance"].startswith("Add now: fakeprint b (1 of 1).")
    assert not any(c[0] != "GET" for c in fake.calls)  # reads only


def test_mcp_faults_levels_are_validated_and_device_scoped():
    fake, t = tools(device_info("a", 0xA1, default=True), device_info("b", 0xB1))
    for bad in ({"nope": True}, {"paper_out": "yes"}, {}):
        with pytest.raises(GuardError):
            t.faults_set("a", **bad)
    with pytest.raises(GuardError):
        t.faults_set("../x", paper_out=True)
    with pytest.raises(Exception, match="unknown device"):
        t.faults_set("ghost", paper_out=True)
    for bad in ({"Black": 101}, {"Black": -1}, {"Black": True}, {"Black": "5"}):
        with pytest.raises(GuardError):
            t.levels_set("b", bad)
    with pytest.raises(GuardError):
        t.levels_set("b")
    with pytest.raises(GuardError):
        t.timing("b", "turbo")
    assert not any(c[0] in ("POST", "DELETE") for c in fake.calls)  # nothing reached the server
    t.levels_set("b", {"Black": 7}, {"Tray1": 0})
    assert fake.calls[-1] == ("POST", "/d/b/v1/levels", {"consumables": {"Black": 7}, "inputs": {"Tray1": 0}})


def test_mcp_tool_surface_never_adds_to_windows(monkeypatch):
    names = {n for n in dir(Tools) if not n.startswith("_")}
    assert names == set(mcp_server.TOOL_NAMES)
    assert not any(k in n for n in names for k in ("add", "install", "settings", "remove", "forget", "reconcile", "shutdown"))
    pytest.importorskip("mcp")
    srv = mcp_server.build_server(BASE)
    import asyncio

    registered = {t.name for t in asyncio.run(srv.list_tools())}
    assert registered == (set(mcp_server.TOOL_NAMES) - {"faults_set"}) | {"faults"}
    assert "never adds a printer" in (srv.instructions or "")


def test_mcp_main_fails_fast_when_nothing_answers(capsys):
    assert mcp_server.main(["--base-url", "http://127.0.0.1:1"]) == 1
    assert "fakeprint-mcp:" in capsys.readouterr().err
    assert mcp_server.main(["--base-url", "http://192.0.2.7:3921"]) == 1
