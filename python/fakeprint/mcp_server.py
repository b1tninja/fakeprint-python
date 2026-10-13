"""fakeprint-mcp: stdio MCP server, attach-only, for a running fakeprint (daemon or embedded).

Guards (identical in spirit to the rest of the lab tooling):

* Attach-only: it never spawns, stops, restarts or kills fakeprint, and fails fast with a clear
  message when nothing answers ``/health``.
* Printers only: it is for fakeprint (ODD ports, 3921...). fakescan has its own MCP server.
* Loopback by default: a non-loopback ``--base-url`` is refused unless ``--allow-remote``.
* NO tool adds anything to Windows (no ``Add-Printer``, no port or driver changes) and none edits
  the registry file. ``queue_guidance`` returns the text a human follows; it opens nothing.
* Device scoped: faults, levels, timing, jobs, captured documents and reset address ONE printer
  (``device`` is the id from ``devices``); inputs are validated (slug ids, known fault names,
  integer levels 0..100, timing presets). Installed printers are never removed or reconfigured, and
  adding/removing printers or moving the install queue is deliberately not offered here (use
  ``fakeprint-ctl`` / ``fakeprint-lab``).

    fakeprint-mcp [--base-url http://127.0.0.1:3921]

Install the optional dependency first: ``pip install mcp``. The tool functions live in
:class:`Tools` so they can be tested without the MCP protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from typing import Any, Mapping

import httpx

from .client import DEFAULT_BASE_URL, Client
from .manager import Manager, ManagerError, UnknownDevice, is_loopback_url

CAVEAT = (
    "Attach-only: reads or drives the running fakeprint; it never spawns or kills a process and never "
    "adds a printer in Windows. Use fakeprint-lab to start the daemon and fakeprint-ctl for the rest."
)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# Tray toggle name -> the key of POST /d/{id}/v1/faults.
FAULT_KEYS = {
    "paper_out": "paper_out", "toner_low": "toner_low", "cover_open": "cover_open",
    "paper_jam": "paper_jam", "offline": "not_accepting_jobs",
}
TIMING_PRESETS = ("instant", "realistic", "default")


class GuardError(RuntimeError):
    """A guard refused the call (nothing was sent)."""


class Tools:
    """The tool implementations; every call checks ``/health`` first (fail fast, never spawn)."""

    def __init__(self, base_url: str, *, allow_remote: bool = False, http: httpx.Client | None = None) -> None:
        self.base = base_url.rstrip("/")
        if not allow_remote and not is_loopback_url(self.base):
            raise GuardError(f"{self.base} is not loopback; pass allow_remote/--allow-remote to attach to another host")
        self._http = http

    # -- plumbing ----------------------------------------------------------------------------
    def _manager(self) -> Manager:
        return Manager(self.base, http=self._http) if self._http is not None else Manager(self.base)

    def _check_alive(self, mgr: Manager) -> None:
        try:
            r = mgr._http.get("/health") if mgr._http is not None else None  # type: ignore[union-attr]
        except httpx.HTTPError as e:
            raise GuardError(
                f"fakeprint not reachable at {self.base} ({type(e).__name__}); start it with `fakeprint-lab ensure`"
            ) from e
        if r is None or r.status_code != 200 or r.text.strip() != "ok":
            raise GuardError(f"fakeprint /health at {self.base} returned {getattr(r, 'text', None)!r}")

    def _client(self, mgr: Manager, device: str) -> Client:
        if not _ID_RE.match(device or ""):
            raise GuardError(f"device {device!r}: ids are 1-32 characters of [A-Za-z0-9_-]")
        dev = next((d for d in mgr.devices() if d.id == device), None)
        if dev is None:
            raise UnknownDevice(f"unknown device {device!r}", request=httpx.Request("GET", self.base), response=httpx.Response(404, request=httpx.Request("GET", self.base)))
        return Client(self.base, device=None if dev.default else device, http=mgr._http)

    def _run(self, fn):  # type: ignore[no-untyped-def]
        mgr = self._manager()
        try:
            self._check_alive(mgr)
            return fn(mgr)
        finally:
            mgr.close()

    # -- tools -------------------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        try:
            self._run(lambda m: None)
            return {"ok": True, "base_url": self.base}
        except GuardError as e:
            return {"ok": False, "base_url": self.base, "error": str(e)}

    def devices(self) -> list[dict[str, Any]]:
        """Every printer: id, name, profile, uuid, endpoints, install state, capability class, live state."""
        def go(m: Manager) -> list[dict[str, Any]]:
            return [
                {
                    "id": d.id, "friendly_name": d.friendly_name, "profile": d.profile, "uuid": d.uuid,
                    "default": d.default, "state": d.state, "needs_readd": d.needs_readd,
                    "reason": d.needs_readd_reason, "class": d.klass, "announcing": d.announcing,
                    "queue_position": d.queue_position, "service_endpoint": d.service_endpoint,
                    "device_endpoint": d.device_endpoint, "ipp_endpoint": d.ipp_endpoint,
                    "inbox_dir": d.inbox_dir, "live": d.live,
                }
                for d in m.devices()
            ]
        return self._run(go)  # type: ignore[no-any-return]

    def queue_guidance(self) -> dict[str, Any]:
        """The install queue and the exact text a human follows to Add the head printer. Opens nothing."""
        def go(m: Manager) -> dict[str, Any]:
            q = m.queue()
            return {"head": q.head, "remaining": q.remaining, "total": q.total, "guidance": m.guidance()}
        return self._run(go)  # type: ignore[no-any-return]

    def status(self, device: str) -> dict[str, Any]:
        return self._run(lambda m: self._client(m, device).status().raw)  # type: ignore[no-any-return]

    def faults_get(self, device: str) -> dict[str, Any]:
        return self._run(lambda m: self._client(m, device).faults())  # type: ignore[no-any-return]

    def faults_set(self, device: str, **flags: Any) -> dict[str, Any]:
        """Toggle tray-style faults on ONE printer: paper_out, toner_low, cover_open, paper_jam, offline
        (= not_accepting_jobs). Pass ``reset=True`` alone to switch all off."""
        body: dict[str, Any] = {}
        for k, v in flags.items():
            if k == "reset":
                if v is True:
                    body["reset"] = True
                continue
            if k not in FAULT_KEYS:
                raise GuardError(f"unknown fault {k!r}; use one of {sorted(FAULT_KEYS)} or reset=True")
            if not isinstance(v, bool):
                raise GuardError(f"fault {k} must be true or false")
            body[FAULT_KEYS[k]] = v
        if not body:
            raise GuardError("nothing to set")
        return self._run(lambda m: self._client(m, device).set_faults(body))  # type: ignore[no-any-return]

    def levels_set(self, device: str, consumables: Mapping[str, int] | None = None, inputs: Mapping[str, int] | None = None) -> dict[str, Any]:
        """Set runtime consumable / input-bin levels (0..100) on ONE printer. Never changes capabilities."""
        for group in (consumables or {}, inputs or {}):
            for k, v in group.items():
                if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100:
                    raise GuardError(f"level {k!r} must be an integer 0..100")
        if not consumables and not inputs:
            raise GuardError("nothing to set")
        return self._run(lambda m: self._client(m, device).levels(dict(consumables or {}), dict(inputs or {})))  # type: ignore[no-any-return]

    def timing(self, device: str, preset: str | None = None) -> dict[str, Any]:
        if preset is not None and preset not in TIMING_PRESETS:
            raise GuardError(f"preset must be one of {TIMING_PRESETS}")
        return self._run(lambda m: self._client(m, device).timing(**({"preset": preset} if preset else {})))  # type: ignore[no-any-return]

    def jobs(self, device: str) -> list[dict[str, Any]]:
        def go(m: Manager) -> list[dict[str, Any]]:
            return [
                {"id": j.job_id, "name": j.name, "user": j.user, "state": j.state, "reasons": j.reasons,
                 "bytes": j.bytes_received, "format": j.document_format}
                for j in self._client(m, device).print_jobs()
            ]
        return self._run(go)  # type: ignore[no-any-return]

    def captured(self, device: str) -> list[dict[str, Any]]:
        """Captured documents of ONE printer (job id, bytes, format, path, peer)."""
        def go(m: Manager) -> list[dict[str, Any]]:
            return [
                {"job_id": c.job_id, "document_id": c.document_id, "bytes": c.bytes, "format": c.document_format,
                 "path": c.path, "peer": c.peer}
                for c in self._client(m, device).jobs()
            ]
        return self._run(go)  # type: ignore[no-any-return]

    def document_info(self, device: str, job_id: int) -> list[dict[str, Any]]:
        """Size, SHA-256 and first bytes (hex) of each captured document of a job: a fingerprint, not the data."""
        from pathlib import Path

        def go(m: Manager) -> list[dict[str, Any]]:
            rows = sorted((c for c in self._client(m, device).jobs() if c.job_id == job_id), key=lambda c: c.document_id)
            out = []
            for c in rows:
                data = Path(c.path).read_bytes()
                out.append({"document_id": c.document_id, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "head_hex": data[:16].hex()})
            return out
        return self._run(go)  # type: ignore[no-any-return]

    def reset(self, device: str) -> dict[str, Any]:
        """Forget ONE printer's jobs/captures/conditions and disarm its injection rules (install state untouched)."""
        return self._run(lambda m: self._client(m, device).reset().raw)  # type: ignore[no-any-return]


def _mcp_class():  # type: ignore[no-untyped-def]
    """``FastMCP`` (mcp 1.x) or ``MCPServer`` (mcp 2.x, same decorator API)."""
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP
    except ImportError as e:  # pragma: no cover - only without the optional dependency
        raise SystemExit("fakeprint-mcp needs the optional `mcp` package: pip install mcp") from e


def build_server(base_url: str, *, allow_remote: bool = False):  # type: ignore[no-untyped-def]
    """The FastMCP server (needs the ``mcp`` package)."""
    FastMCP = _mcp_class()

    t = Tools(base_url, allow_remote=allow_remote)
    mcp = FastMCP(
        "fakeprint-mcp",
        instructions=(
            f"Attach-only MCP for fakeprint at {t.base}. fakeprint uses ODD ports (3921, 3923, ...); "
            "fakescan (even ports) has its own MCP. Never spawns or kills fakeprint and never adds a printer "
            "in Windows: a human Adds printers, one at a time, following queue_guidance."
        ),
    )

    def tool(fn):  # type: ignore[no-untyped-def]
        def wrapped(*a: Any, **k: Any) -> Any:
            try:
                return fn(*a, **k)
            except (GuardError, ManagerError, httpx.HTTPError, KeyError, OSError, ValueError) as e:
                return {"error": str(e)}

        wrapped.__name__, wrapped.__doc__ = fn.__name__, (fn.__doc__ or fn.__name__) + " " + CAVEAT
        return mcp.tool()(wrapped)

    for name in ("health", "devices", "queue_guidance", "status", "faults_get", "levels_set", "timing", "jobs",
                 "captured", "document_info", "reset"):
        tool(getattr(t, name))

    @mcp.tool(description="Toggle faults on ONE printer. " + CAVEAT)
    def faults(device: str, paper_out: bool | None = None, toner_low: bool | None = None, cover_open: bool | None = None,
               paper_jam: bool | None = None, offline: bool | None = None, reset: bool = False) -> Any:
        try:
            flags = {k: v for k, v in dict(paper_out=paper_out, toner_low=toner_low, cover_open=cover_open,
                                           paper_jam=paper_jam, offline=offline).items() if v is not None}
            if reset:
                flags["reset"] = True
            return t.faults_set(device, **flags)
        except (GuardError, ManagerError, httpx.HTTPError, KeyError, OSError, ValueError) as e:
            return {"error": str(e)}

    return mcp


TOOL_NAMES = (
    "health", "devices", "queue_guidance", "status", "faults_get", "faults_set", "levels_set", "timing", "jobs",
    "captured", "document_info", "reset",
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fakeprint-mcp", description="fakeprint MCP server (stdio, attach-only)")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="fakeprint base URL (default %(default)s)")
    p.add_argument("--allow-remote", action="store_true", help="allow a non-loopback --base-url")
    args = p.parse_args(argv)
    try:
        tools = Tools(args.base_url, allow_remote=args.allow_remote)
    except GuardError as e:
        print(f"fakeprint-mcp: {e}", file=sys.stderr)
        return 1
    h = tools.health()
    if not h["ok"]:
        print(f"fakeprint-mcp: {h['error']}", file=sys.stderr)
        return 1
    build_server(args.base_url, allow_remote=args.allow_remote).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
