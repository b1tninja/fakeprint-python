"""The embedded server: lifecycle, identity, control plane, isolation between instances."""

from __future__ import annotations

import concurrent.futures as cf
import socket

import httpx
import pytest

from fakeprint.client import ApiError, Client

pytestmark = pytest.mark.embedded


def test_start_and_stop(server_cls):
    s = server_cls()
    assert s.running
    assert s.port > 0
    assert s.base_url == f"http://127.0.0.1:{s.port}"
    with Client(s.base_url) as c:
        assert c.health()
    inbox = s.inbox_dir
    assert inbox.is_dir()
    s.stop()
    assert not s.running
    assert not inbox.exists()  # a private inbox is removed with the server
    s.stop()  # idempotent
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://127.0.0.1:{s.port}/health", timeout=2)


def test_context_manager(server_cls):
    with server_cls() as s:
        assert s.running
        port = s.port
    assert not s.running
    with socket.socket() as sock:
        sock.settimeout(1)
        assert sock.connect_ex(("127.0.0.1", port)) != 0


def test_loopback_only(server_cls):
    for host in ("0.0.0.0", "192.168.50.20", "::", "example.com"):
        with pytest.raises((ValueError, RuntimeError)):
            server_cls(host=host)


def test_bad_profile_and_uuid(server_cls):
    with pytest.raises(RuntimeError, match="failed to start"):
        server_cls(profile="no-such-profile")
    with pytest.raises(ValueError):
        server_cls(device_uuid="not-a-uuid")


def test_custom_inbox_is_kept(server_cls, tmp_path):
    inbox = tmp_path / "in"
    with server_cls(inbox=inbox) as s:
        s.printer().print_document(b"keep me", format="application/octet-stream")
    assert [p.read_bytes() for p in inbox.glob("*.bin")] == [b"keep me"]


def test_status_identity(srv):
    st = srv.status()
    assert st.profile_id == "generic-wsd"
    assert st.device_endpoint == srv.device_endpoint
    assert st.service_endpoint == srv.service_endpoint
    assert st.device_uuid == srv.device_uuid
    assert srv.ipp_uuid != srv.device_uuid  # distinct IPP uuid (see the library's lab.rs)
    assert "application/pdf" in st.document_formats
    assert st.active_jobs == 0 and st.captured_jobs == 0
    assert "generic-wsd" in st.available_profiles


def test_explicit_uuid_and_name(server_cls):
    u = "11111111-2222-4333-8444-555555555555"
    with server_cls(device_uuid=u, name="unit") as s:
        assert s.device_uuid == u
        assert s.status().name == "unit"


def test_control_plane_matches_native_state(srv, pc):
    job = pc.print_document(b"abc", format="application/octet-stream")
    with srv.client() as c:
        st = c.status()
        assert st.wsd_actions.get("CreatePrintJob") == 1
        assert st.wsd_actions.get("SendDocument") == 1
        assert st.captured_jobs == 1
        rows = c.jobs()
        assert [(r.job_id, r.bytes) for r in rows] == [(job, 3)]
        assert [(r.job_id, r.bytes) for r in srv.captured()] == [(job, 3)]
        info = c.ctl_info()
        assert "status" in info["cmds"]
        assert c.ctl("jobs")["jobs"][0]["job_id"] == job
        assert c.ctl("health")["ok"] is True
        with pytest.raises(ApiError):
            c.ctl("nope")
        with pytest.raises(ApiError):
            c.ctl("profile")  # args.id required
        c.reset()
        assert c.jobs() == []
    assert srv.jobs() == []


def test_set_profile(srv):
    srv.set_profile("generic-wsd")
    with pytest.raises(KeyError):
        srv.set_profile("does-not-exist")
    with srv.client() as c:
        assert c.set_profile("generic-wsd").profile_id == "generic-wsd"
        with pytest.raises(ApiError):
            c.set_profile("does-not-exist")


def test_two_servers_are_independent(server_cls):
    with server_cls() as a, server_cls() as b:
        assert a.port != b.port
        assert a.device_uuid != b.device_uuid
        assert a.inbox_dir != b.inbox_dir
        ja = a.printer().print_document(b"for-a", format="application/octet-stream")
        assert b.jobs() == [] and b.captured() == []
        jb = b.printer().print_document(b"for-b-longer", format="application/octet-stream")
        assert a.documents(ja) == [b"for-a"]
        assert b.documents(jb) == [b"for-b-longer"]
        # fault on one does not touch the other
        a.inject_fault("*", count=None)
        b.printer().get_printer_elements()
        a.stop()
        assert b.running
        assert b.printer().get_active_jobs() == []


def test_concurrent_servers_and_clients(server_cls):
    payloads = {i: bytes([i]) * (1000 + i) for i in range(12)}

    def run(server, ids):
        pc = server.printer()
        got = {}
        for i in ids:
            got[i] = pc.print_document(payloads[i], format="application/octet-stream", mtom=bool(i % 2))
        return got

    with server_cls() as a, server_cls() as b, cf.ThreadPoolExecutor(8) as ex:
        futs = [(a, ex.submit(run, a, range(0, 6))), (b, ex.submit(run, b, range(6, 12)))]
        for server, fut in futs:
            for i, job in fut.result().items():
                assert server.documents(job) == [payloads[i]], (i, job)
        assert len(a.captured()) == 6 and len(b.captured()) == 6
        assert all(j.state == "Completed" for j in a.jobs() + b.jobs())


def test_many_threads_one_server(srv):
    def one(i):
        data = f"doc-{i}".encode() * 50
        return i, data, srv.printer().print_document(data, format="application/octet-stream")

    with cf.ThreadPoolExecutor(8) as ex:
        results = list(ex.map(one, range(24)))
    assert len({job for _, _, job in results}) == 24  # job ids unique
    for _, data, job in results:
        assert srv.documents(job) == [data]
