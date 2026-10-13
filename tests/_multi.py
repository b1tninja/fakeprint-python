"""Test helpers: DeviceInfo fixtures and an in-memory FAKE of the daemon's manager routes.

``FakeDaemon`` is not fakeprint: it mimics docs/multi-device.md (2.1 route table, 2.2 DeviceInfo, 3.3 queue,
3.4 matching) closely enough to exercise ``Manager``, ``lab`` and the MCP tools without a server. What
it proves is that the Python speaks the contract; the real thing is exercised by ``test_multi_device_embedded``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from fakeprint.registry import match_kind

BASE = "http://127.0.0.1:3921"


def uuid_for(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012x}"


def device_info(id: str = "mono", n: int = 0xA1, **over: Any) -> dict[str, Any]:
    """A section 2.2 ``DeviceInfo`` row."""
    default = over.pop("default", False)
    base = "" if default else f"/d/{id}"
    row: dict[str, Any] = {
        "id": id,
        "default": default,
        "uuid": uuid_for(n),
        "ipp_uuid": uuid_for(n + 1),
        "profile": "mono-laser",
        "friendly_name": f"fakeprint {id}",
        "base_path": base,
        "device_endpoint": f"{BASE}{base}/WebServices/Device",
        "service_endpoint": f"{BASE}{base}/WebServices/PrinterService",
        "ipp_endpoint": f"ipp://127.0.0.1:3921{base}/ipp/print",
        "jetdirect_port": None,
        "inbox_dir": f"C:\\inbox\\{id}",
        "announcing": False,
        "queue_position": None,
        "needs_readd": False,
        "needs_readd_reason": None,
        "install": {"state": "new"},
        "config": {
            "fingerprint": "fnv64:0123456789abcdef",
            "published_fingerprint": "fnv64:3fa9c2d1e07b4455",
            "class": "3fa9c2d1e07b",
            "summary": {
                "formats": ["application/pdf"], "color_modes": ["Monochrome"], "sides": ["OneSided"],
                "media_sizes": ["iso_a4_210x297mm"], "input_bins": ["Tray1"],
                "versions": {"v11": False, "v12": False, "v20": False},
            },
        },
        "live": {
            "printer_state": "Idle", "state_reasons": ["none"], "accepting_jobs": True, "active_jobs": 0,
            "captured_jobs": 0, "active_job": None, "last_job": None,
            "faults": {"paper_out": False, "toner_low": False, "cover_open": False, "paper_jam": False, "offline": False},
            "timing_preset": "default", "consumables": [{"name": "Black", "level": 80}], "inputs": [{"name": "Tray1", "level": 100}],
        },
    }
    row.update(over)
    return row


class FakeDaemon:
    """In-memory manager routes behind an ``httpx.MockTransport`` (``fake.client()`` / ``fake.transport``)."""

    def __init__(self, *devices: dict[str, Any], missing: tuple[str, ...] = (), loopback: bool = True) -> None:
        self.devices: list[dict[str, Any]] = [dict(d) for d in devices]
        self.calls: list[tuple[str, str, Any]] = []
        self.missing = missing  # path prefixes answered with an empty 404 (route not in this build)
        self.loopback = loopback
        self.opened_settings = 0
        self.order: list[str] | None = None
        self.transport = httpx.MockTransport(self.handle)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=BASE, transport=self.transport)

    # -- helpers
    def _find(self, id: str) -> dict[str, Any] | None:
        return next((d for d in self.devices if d["id"] == id), None)

    def _queue(self) -> dict[str, Any]:
        ids = [d["id"] for d in self.devices if d["install"]["state"] in ("new", "probed", "stale")
               and (d["install"].get("reason") or "") != "not listed by Windows"]
        if self.order:
            ids = [i for i in self.order if i in ids] + [i for i in ids if i not in self.order]
        for d in self.devices:
            d["announcing"] = d["install"]["state"] == "installed" or (bool(ids) and d["id"] == ids[0])
            d["queue_position"] = ids.index(d["id"]) + 1 if d["id"] in ids else None
        return {
            "head": ids[0] if ids else None, "remaining": ids[1:], "position": 1 if ids else None, "total": len(ids),
            "head_since": None, "head_timeout_s": 180, "settle_s": 5,
        }

    @staticmethod
    def _j(status: int, doc: Any) -> httpx.Response:
        return httpx.Response(status, json=doc)

    def handle(self, req: httpx.Request) -> httpx.Response:  # noqa: C901
        path = req.url.path
        body: Any = json.loads(req.content) if req.content else None
        self.calls.append((req.method, path + (f"?{req.url.query.decode()}" if req.url.query else ""), body))
        if any(path.startswith(m) for m in self.missing):
            return httpx.Response(404)
        if path == "/health":
            return httpx.Response(200, text="ok")
        mutation = req.method != "GET" and (path.startswith(("/v1/devices", "/v1/queue", "/v1/reconcile")))
        if mutation and not self.loopback:
            return self._j(403, {"error": "loopback peers only"})
        self._queue()
        if path == "/v1/devices" and req.method == "GET":
            return self._j(200, self.devices)
        if path == "/v1/devices" and req.method == "POST":
            if not body.get("id") or not body.get("profile"):
                return self._j(400, {"error": "id and profile required"})
            if self._find(body["id"]):
                return self._j(409, {"error": f"duplicate device id {body['id']!r}"})
            n = 0xB0 + 2 * len(self.devices)
            row = device_info(body["id"], n, profile=body["profile"], default=not self.devices)
            if body.get("device_uuid"):
                row["uuid"] = body["device_uuid"]
            if body.get("friendly_name"):
                row["friendly_name"] = body["friendly_name"]
            self.devices.append(row)
            self._queue()
            return self._j(201, row)
        if path == "/v1/queue" and req.method == "GET":
            return self._j(200, self._queue())
        if path == "/v1/queue" and req.method == "POST":
            self.order = list(body["order"])
            return self._j(200, self._queue())
        if path == "/v1/queue/skip":
            q = self._queue()
            if q["head"]:
                self.order = q["remaining"] + [q["head"]]
            return self._j(200, self._queue())
        if path == "/v1/queue/advance":
            return self._j(200, self._queue())
        if path == "/v1/registry":
            return self._j(200, {"devices": [{"name": d["id"], "device_uuid": d["uuid"], "profile": d["profile"],
                                              "install": d["install"]} for d in self.devices]})
        if path == "/v1/profiles":
            return self._j(200, [{"id": "mono-laser", "label": "Mono laser", "formats": ["application/pdf"]},
                                 {"id": "color-inkjet-duplex", "label": "Color inkjet"}])
        if path == "/v1/reconcile":
            printers = body["printers"]
            installed, foreign, seen = [], [], set()
            for p in printers:
                hit = next((d for d in self.devices if match_kind(d, p)), None)
                if hit is None or hit["id"] in seen:
                    foreign.append(p)
                    continue
                seen.add(hit["id"])
                installed.append(hit["id"])
                hit["install"] = {"state": "installed", "kind": match_kind(hit, p), "windows": p}
            stale = [d["id"] for d in self.devices if d["install"]["state"] == "installed" and d["id"] not in seen]
            for i in stale:
                self._find(i)["install"].update(state="stale", reason="not listed by Windows")  # type: ignore[index]
            return self._j(200, {"installed": installed, "stale": stale, "foreign": foreign})
        if path == "/v1/ctl" and req.method == "POST":
            if body["cmd"] == "open_settings":
                self.opened_settings += 1
                return self._j(200, {"opened": True})
            if body["cmd"] == "mark_added":
                d = self._find(body["args"]["device"])
                if d is None:
                    return self._j(404, {"error": "unknown device"})
                d["install"] = {"state": "installed", "note": "legacy added.pnp"}
                return self._j(200, {"ok": True})
            if body["cmd"] in ("faults", "reset"):
                return self._j(200, {"cmd": body["cmd"], "args": body["args"]})
            return self._j(400, {"error": f"unknown ctl command {body['cmd']!r}"})
        if path.startswith("/v1/devices/"):
            rest = path[len("/v1/devices/"):].split("/")
            d = self._find(rest[0])
            if d is None:
                return self._j(404, {"error": "unknown device"})
            if len(rest) == 1 and req.method == "DELETE":
                self.devices.remove(d)
                return httpx.Response(204)
            if rest[1:] == ["reconfigure"]:
                d["profile"] = body["profile"]
                return self._j(200, d)
            if rest[1:] == ["forget"]:
                d["install"] = {"state": "new"}
                return self._j(200, d)
            if rest[1:] == ["installed"]:
                kind = match_kind(d, body["windows"])
                if kind is None:
                    return self._j(400, {"error": "port does not identify this device"})
                d["install"] = {"state": "installed", "kind": kind, "windows": body["windows"]}
                return self._j(200, d)
        if path.startswith("/d/") and path.endswith("/v1/levels"):
            d = self._find(path.split("/")[2])
            if d is None:
                return self._j(404, {"error": "unknown device"})
            for k, v in (body.get("consumables") or {}).items():
                for c in d["live"]["consumables"]:
                    if c["name"] == k:
                        c["level"] = v
            return self._j(200, d["live"])
        return httpx.Response(404)
