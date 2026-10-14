"""``fakeprint.Manager``: client of the device-manager surface of a running fakeprint.

Talks to a ``fakeprint --manage`` daemon (or an embedded :class:`fakeprint.Server`) over HTTP
(``/v1/devices``, ``/v1/queue``, ``/v1/registry``, ``/v1/reconcile``, ...) or over the daemon's named
pipe (``\\\\.\\pipe\\fakeprint-<name>``, the same JSON-line ``ctl`` commands). Every mutation goes to
the daemon, which owns the registry file (Rust file lock + atomic writer); Python never writes
``devices.json`` and never adds anything in Windows: ``open_settings`` only opens
``ms-settings:printers`` and the Add stays a human click.

Errors are :class:`ManagerError` subclasses of :class:`fakeprint.ApiError` (an
``httpx.HTTPStatusError``), typed by status: :class:`BadRequest` (400), :class:`Forbidden` (403,
mutations are loopback-only), :class:`UnknownDevice` (404), :class:`Conflict` (409, duplicate id or
UUID) and :class:`NotYet` (501, or a manager route this fakeprint build does not serve yet).
"""

from __future__ import annotations

import ipaddress
import json
import os
import time
import webbrowser
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlsplit

import httpx

from . import _paths
from .client import ApiError, Client
from .registry import (
    ManagedDevice,
    QueueInfo,
    ReconcileResult,
    Registry,
    WindowsPrinter,
    match_kind,
    match_printers_to_devices,
    port_uuids,
)
from .ui import ProfileInfo, StatusRow, profiles_from_files
from .ui import summary_level as _summary_level


# ---------------------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------------------


class ManagerError(ApiError):
    """A manager call failed; ``str(e)`` is the server's text, ``e.status_code`` the HTTP status."""

    @property
    def status_code(self) -> int:
        return self.response.status_code


class BadRequest(ManagerError):
    """400: bad input, or an ``installed`` whose port does not prove the device."""


class Forbidden(ManagerError):
    """403: the call needs a loopback peer (device-set and manager mutations are loopback-only)."""


class UnknownDevice(ManagerError, KeyError):
    """404 ``unknown device``."""

    def __str__(self) -> str:  # KeyError would repr() the message
        return Exception.__str__(self)


class Conflict(ManagerError):
    """409: duplicate id, UUID or UUID/IPP-UUID clash."""


class NotYet(ManagerError):
    """501 (or a manager route missing from this build): specified, not implemented yet."""


_BY_STATUS: dict[int, type[ManagerError]] = {
    400: BadRequest, 403: Forbidden, 404: UnknownDevice, 409: Conflict, 501: NotYet,
}


def error_text(doc: Any) -> str:
    if isinstance(doc, Mapping):
        return str(doc.get("error") or doc.get("message") or json.dumps(doc))
    return str(doc).strip()


def make_error(status: int, doc: Any, method: str, url: str) -> ManagerError:
    """Build the typed error for an HTTP-style answer. A 404/405 without an ``error`` message on a
    manager route means the route itself is missing (an older fakeprint build): that is ``NotYet``."""
    text = error_text(doc)
    cls = _BY_STATUS.get(status, ManagerError)
    if status in (404, 405) and not (isinstance(doc, Mapping) and doc.get("error")):
        cls, text = NotYet, f"{method} {url}: this fakeprint build does not serve that route yet"
    req = httpx.Request(method, url if "://" in url else f"http://manager.invalid{url}")
    resp = httpx.Response(status, request=req, text=text)
    return cls(f"{method} {url}: {status} {text[:300]}", request=req, response=resp)


# ---------------------------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------------------------


def is_loopback_url(url: str) -> bool:
    """True when ``url``'s host is ``localhost`` or a loopback IP."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _status_from_error(text: str) -> int:
    t = text.lower()
    if "not implemented yet" in t or "not yet" in t:
        return 501
    if "unknown device" in t or "not found" in t:
        return 404
    if "duplicate" in t or "already" in t or "conflict" in t or "clash" in t:
        return 409
    if "loopback" in t or "forbidden" in t:
        return 403
    return 400


class PipeTransport:
    """The daemon's named pipe: one JSON line ``{"cmd", "args"}`` per request, one JSON line
    ``{"ok", "result" | "error"}`` back (the same dispatcher as ``POST /v1/ctl``). Windows only.

    ``opener(path)`` returns a binary file-like with ``write``/``readline``/``close`` (tests pass a
    fake; the default is ``open(path, "r+b", buffering=0)``). A request blocks until the daemon
    answers; the daemon answers per request, so there is no idle wait.
    """

    def __init__(self, name: str = "default", *, opener: Callable[[str], Any] | None = None) -> None:
        self.name = name
        self.path = rf"\\.\pipe\fakeprint-{name}"
        self._opener = opener or (lambda p: open(p, "r+b", buffering=0))

    def ctl(self, cmd: str, args: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        f = self._opener(self.path)
        try:
            body: dict[str, Any] = {"cmd": cmd}
            if args:
                body["args"] = dict(args)
            f.write((json.dumps(body) + "\n").encode("utf-8"))
            if hasattr(f, "flush"):
                f.flush()
            line = f.readline()
        finally:
            f.close()
        if not line:
            raise OSError(f"{self.path}: closed without an answer")
        resp = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
        if resp.get("ok"):
            return 200, resp.get("result")
        text = str(resp.get("error") or "error")
        return _status_from_error(text), {"error": text}

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------------------------


class Manager:
    """Typed client for the manager endpoints of a fakeprint daemon or embedded server.

    ``Manager("http://127.0.0.1:3921")``, ``Manager(client)``, ``Manager(server)`` (anything with a
    ``base_url``, e.g. :class:`fakeprint.Server`, whose ``.manager`` is exactly this) or
    ``Manager.pipe("default")`` for the named pipe. ``http=`` supplies the ``httpx.Client`` (tests
    give one with ``httpx.MockTransport``); ``pipe=`` a :class:`PipeTransport`.
    """

    def __init__(
        self,
        target: str | Client | Any = "http://127.0.0.1:3921",
        *,
        timeout: float = 30.0,
        http: httpx.Client | None = None,
        pipe: PipeTransport | None = None,
    ) -> None:
        self._pipe = pipe
        if isinstance(target, PipeTransport):
            self._pipe, target = target, None
        self._own_http = False
        if self._pipe is not None:
            self.base_url = ""
            self._http = None
            return
        if isinstance(target, Client):
            url, http = target.base_url, http or target._http
        else:
            url = target if isinstance(target, str) else target.base_url
        self.base_url = url.rstrip("/")
        if http is None:
            http, self._own_http = httpx.Client(base_url=self.base_url, timeout=timeout), True
        self._http = http

    @classmethod
    def pipe(cls, name: str = "default", **kw: Any) -> Manager:
        """A manager over the named pipe ``\\\\.\\pipe\\fakeprint-<name>`` (Windows)."""
        return cls(pipe=PipeTransport(name, **kw))

    # -- lifecycle -------------------------------------------------------------------------
    def close(self) -> None:
        if self._http is not None and self._own_http:
            self._http.close()

    def __enter__(self) -> Manager:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Manager({self._pipe.path if self._pipe else self.base_url})"

    # -- the one call path -----------------------------------------------------------------
    def _call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        ctl: tuple[str, Mapping[str, Any]] | None = None,
        params: Mapping[str, str] | None = None,
        raw: bool = False,
    ) -> Any:
        """One manager call: HTTP ``method path`` or, over the pipe, the equivalent ``ctl`` command."""
        if self._pipe is not None:
            if ctl is None:
                raise NotYet(
                    f"{method} {path} has no ctl command, so it is not available over the pipe",
                    request=httpx.Request(method, "pipe://" + self._pipe.name),
                    response=httpx.Response(501, request=httpx.Request(method, "pipe://" + self._pipe.name)),
                )
            status, doc = self._pipe.ctl(*ctl)
            url = f"pipe://{self._pipe.name}/{ctl[0]}"
        else:
            assert self._http is not None
            r = self._http.request(method, path, params=params, json=body if body is not None else None)
            status, url = r.status_code, str(r.request.url)
            try:
                doc = r.json()
            except ValueError:
                doc = r.text
        if status >= 400:
            raise make_error(status, doc, method, url)
        return doc

    def ctl(self, cmd: str, **args: Any) -> Any:
        """Any named ctl command (``POST /v1/ctl`` or the pipe); see docs/multi-device.md 4.7."""
        return self._call("POST", "/v1/ctl", {"cmd": cmd, "args": args}, ctl=(cmd, args))

    # -- reads -----------------------------------------------------------------------------
    def devices(self) -> list[ManagedDevice]:
        """``GET /v1/devices``: every printer with install state, config, ``needs_readd`` and live state."""
        doc = self._call("GET", "/v1/devices", ctl=("devices", {}))
        if isinstance(doc, Mapping):
            doc = doc.get("devices", [])
        return [ManagedDevice.parse(r) for r in doc or [] if isinstance(r, Mapping)]

    list = devices

    def device(self, id: str) -> ManagedDevice:
        for d in self.devices():
            if d.id == id:
                return d
        raise make_error(404, {"error": f"unknown device {id!r}"}, "GET", "/v1/devices")

    def registry(self) -> Registry:
        """``GET /v1/registry`` (loopback only): the document the daemon holds (in memory when embedded)."""
        return Registry.parse(self._call("GET", "/v1/registry", ctl=("registry", {})))

    def queue(self) -> QueueInfo:
        """``GET /v1/queue``: the serial install queue (head, remaining, position, timers)."""
        return QueueInfo.parse(self._call("GET", "/v1/queue", ctl=("queue", {})))

    # -- device CRUD -----------------------------------------------------------------------
    def add_info(
        self,
        id: str,
        profile: str,
        device_uuid: str | None = None,
        friendly_name: str | None = None,
        jetdirect_port: int | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/devices``; returns the ``DeviceInfo`` object as served."""
        body: dict[str, Any] = {"id": id, "profile": profile}
        if device_uuid:
            body["device_uuid"] = device_uuid
        if friendly_name:
            body["friendly_name"] = friendly_name
        if jetdirect_port is not None:
            body["jetdirect_port"] = jetdirect_port
        doc = self._call("POST", "/v1/devices", body, ctl=("device_add", body))
        return dict(doc) if isinstance(doc, Mapping) else {}

    def add(
        self,
        id: str,
        profile: str,
        device_uuid: str | None = None,
        friendly_name: str | None = None,
        jetdirect_port: int | None = None,
    ) -> ManagedDevice:
        """Add a printer (``POST /v1/devices``). Without ``device_uuid`` the daemon reuses the row's
        stored UUID, else derives a stable one. A new printer is dormant until it reaches the queue
        head, so adding several cannot flood Windows; this never adds anything in Windows."""
        return ManagedDevice.parse(self.add_info(id, profile, device_uuid, friendly_name, jetdirect_port))

    def remove(self, id: str, forget: bool = False) -> None:
        """``DELETE /v1/devices/{id}[?forget=true]``: stop it (Bye if announcing); the registry row
        stays unless ``forget``."""
        self._call(
            "DELETE", f"/v1/devices/{quote(id, safe='')}", params={"forget": "true"} if forget else None,
            ctl=("device_remove", {"device": id, "forget": forget}),
        )

    def reconfigure(self, id: str, profile: str) -> ManagedDevice:
        """Swap the profile. An installed printer becomes ``stale`` (needs_readd) only when the
        PUBLISHED capabilities change, never for runtime-only changes."""
        return ManagedDevice.parse(
            self._call(
                "POST", f"/v1/devices/{quote(id, safe='')}/reconfigure", {"profile": profile},
                ctl=("reconfigure", {"device": id, "profile": profile}),
            )
        )

    # -- install tracking ------------------------------------------------------------------
    def installed(
        self,
        id: str,
        windows: Mapping[str, Any] | WindowsPrinter | Any,
        kind: str | None = None,
        *,
        check: bool = True,
    ) -> ManagedDevice:
        """Record that Windows has this printer (``POST /v1/devices/{id}/installed``).

        The daemon refuses unless the printer's port proves the device (the section 3.4 rule).
        With ``check`` the same rule is applied here first and raises :class:`BadRequest` without
        sending anything, so an ambiguous or foreign printer can never be recorded.
        """
        win = WindowsPrinter.parse(windows)
        if check:
            dev = self.device(id)
            if match_kind(dev, win) is None:
                raise make_error(
                    400,
                    {"error": f"port {win.port!r} does not identify device {id!r} (one UUID equal to {dev.uuid}, or its IPP URL)"},
                    "POST", f"/v1/devices/{id}/installed",
                )
        body: dict[str, Any] = {"windows": win.to_dict()}
        if kind:
            body["kind"] = kind
        return ManagedDevice.parse(
            self._call("POST", f"/v1/devices/{quote(id, safe='')}/installed", body, ctl=("installed", {"device": id, **body}))
        )

    def mark_added(self, id: str) -> Any:
        """ctl ``mark_added``: record a legacy (``added.pnp``) install for ``id``. Only for a printer a
        human has already Added; nothing is installed by this call."""
        return self.ctl("mark_added", device=id)

    def forget(self, id: str) -> ManagedDevice:
        """``POST /v1/devices/{id}/forget``: drop install info, keep the row."""
        return ManagedDevice.parse(
            self._call("POST", f"/v1/devices/{quote(id, safe='')}/forget", {}, ctl=("forget", {"device": id}))
        )

    def reconcile(self, printers: Iterable[Mapping[str, Any] | WindowsPrinter | Any]) -> ReconcileResult:
        """``POST /v1/reconcile`` with the Windows printer listing (read-only on the Windows side).

        Rows whose port names more than one UUID are withheld and reported as foreign: they can
        never be recorded as installed through this call, whatever the daemon's own check does.
        """
        rows = [WindowsPrinter.parse(p) for p in printers]
        ambiguous = [w for w in rows if len(port_uuids(w.port)) > 1]
        sendable = [w for w in rows if len(port_uuids(w.port)) <= 1]
        body = {"printers": [w.to_dict() for w in sendable]}
        res = ReconcileResult.parse(self._call("POST", "/v1/reconcile", body, ctl=("reconcile", body)))
        res.foreign.extend(ambiguous)
        return res

    def match(self, printers: Iterable[Any]):
        """Pure local classification (nothing is changed): see ``registry.match_printers_to_devices``."""
        return match_printers_to_devices(self.devices(), printers)

    def wait_installed(
        self,
        id: str,
        timeout: float = 60.0,
        interval: float = 0.5,
        *,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> ManagedDevice:
        """Poll until ``id`` is ``installed``; ``TimeoutError`` otherwise. Bounded, never adds anything."""
        deadline = clock() + timeout
        while True:
            dev = self.device(id)
            if dev.state == "installed":
                return dev
            if clock() >= deadline:
                raise TimeoutError(f"device {id!r} still {dev.state!r} after {timeout}s")
            sleep(interval)

    def reconcile_from_windows(self, list_printers: Callable[[], Iterable[Any]] | None = None) -> ReconcileResult:
        """List Windows printers (read-only) and reconcile. Uses ``fakeprint.winprint.list_printers``
        when the module provides it (preferring ``describe_printers``, which adds each printer's
        ``location`` and, for ``WSD-`` ports, the ``wsd_uuid`` read from the WSD Port registry key), or
        ``list_printers`` (a callable returning mappings/objects with ``name``/``port``/``driver`` and
        optionally ``location``/``wsd_uuid``). Changes nothing on Windows."""
        if list_printers is None:
            try:
                from . import winprint

                list_printers = getattr(winprint, "describe_printers", None) or winprint.list_printers
            except (ImportError, AttributeError) as e:
                raise RuntimeError(
                    "fakeprint.winprint.list_printers is not available; pass list_printers=... to reconcile_from_windows"
                ) from e
        return self.reconcile(list_printers())

    # -- runtime state ---------------------------------------------------------------------
    def levels(
        self, id: str, consumables: Mapping[str, int] | None = None, inputs: Mapping[str, int] | None = None
    ) -> dict[str, Any]:
        """``POST /d/{id}/v1/levels``: runtime consumable/bin levels of one printer (never a capability
        change). Returns the ``live`` block."""
        body: dict[str, Any] = {}
        if consumables:
            body["consumables"] = dict(consumables)
        if inputs:
            body["inputs"] = dict(inputs)
        path = f"/d/{quote(id, safe='')}/v1/levels"
        return dict(self._call("POST", path, body, ctl=("levels", {"device": id, **body})))

    # -- queue -----------------------------------------------------------------------------
    def advance(self) -> QueueInfo:
        """``POST /v1/queue/advance``: promote the next printer now (a human has Added the head)."""
        return QueueInfo.parse(self._call("POST", "/v1/queue/advance", {}, ctl=("queue_advance", {})))

    def skip(self) -> QueueInfo:
        """``POST /v1/queue/skip``: rotate the head to the back."""
        return QueueInfo.parse(self._call("POST", "/v1/queue/skip", {}, ctl=("queue_skip", {})))

    def reorder(self, ids: Iterable[str]) -> QueueInfo:
        """``POST /v1/queue`` ``{order: [ids]}``: set the install order."""
        order = list(ids)
        return QueueInfo.parse(self._call("POST", "/v1/queue", {"order": order}, ctl=("queue_reorder", {"order": order})))

    # -- UI / tray parity ------------------------------------------------------------------
    def profiles(self) -> list[ProfileInfo]:
        """``GET /v1/profiles`` as typed :class:`~fakeprint.ui.ProfileInfo`.

        FALLBACK (best effort): a daemon without the route answers 404; then the ``*.json`` files of
        the profiles directory this package knows are read locally (``source == "files"``; they
        describe the local checkout, which may differ from what a remote daemon has)."""
        try:
            data = self._call("GET", "/v1/profiles", ctl=("profiles", {}))
        except NotYet:
            return profiles_from_files(_paths.profiles_dir())
        if isinstance(data, Mapping):
            data = data.get("profiles", [])
        if not isinstance(data, list):
            return profiles_from_files(_paths.profiles_dir())
        return [ProfileInfo.parse(x) for x in data if isinstance(x, Mapping)]

    @property
    def ui_url(self) -> str:
        return f"{self.base_url}/ui"

    def open_ui(self, opener: Callable[[str], Any] | None = None) -> str:
        """Open the daemon's ``/ui`` page in the default browser (``webbrowser.open`` unless ``opener``
        is given) and return the URL. The page is loopback-only on the server side."""
        if self._pipe is not None:
            raise ValueError("open_ui needs an HTTP manager (the pipe has no URL)")
        url = self.ui_url
        (opener or webbrowser.open)(url)
        return url

    def open_settings(self, page: str = "printers") -> bool:
        """ctl ``open_settings``: the daemon opens ``ms-settings:printers`` in its own session.
        ``True`` when it did; ``False`` when it did not or the daemon cannot (older build, not
        loopback). Never raises for a missing route. It opens a page only: the Add is a human click."""
        if self._http is not None and not is_loopback_url(self.base_url):
            return False
        try:
            res = self.ctl("open_settings", page=page)
        except (ManagerError, httpx.HTTPError, OSError, ValueError):
            return False
        inner = res.get("result") if isinstance(res, Mapping) and isinstance(res.get("result"), Mapping) else res
        return bool(isinstance(inner, Mapping) and inner.get("opened") is True)

    def open_add_device_settings(self, opener: Callable[[str], Any] | None = None, *, use_daemon: bool = True) -> str:
        """Open Windows' Printers & scanners page (where a printer is Added). Returns ``"daemon"``
        (a loopback daemon opened it), ``"startfile"`` (``opener('ms-settings:printers')``, default
        ``os.startfile``, Windows only) or ``"unavailable"``. Never raises for a missing route."""
        if use_daemon and self.open_settings("printers"):
            return "daemon"
        if opener is None and os.name == "nt":
            opener = os.startfile  # type: ignore[attr-defined]
        if opener is None:
            return "unavailable"
        opener("ms-settings:printers")
        return "startfile"

    def status_table(self) -> list[StatusRow]:
        """One :class:`~fakeprint.ui.StatusRow` per printer, from ``GET /v1/devices`` (the ``live``
        block carries state, faults and job counts). Tolerant of missing fields; raises when the
        device list itself fails."""
        return [StatusRow.from_device(d) for d in self.devices()]

    def summary_level(self) -> str:
        """``green`` / ``amber`` / ``red`` as the tray icon shows it; ``red`` when the daemon is
        unreachable or answers with an error."""
        try:
            rows: list[StatusRow] | None = self.status_table()
        except (httpx.HTTPError, ManagerError, OSError, ValueError):
            rows = None
        return _summary_level(rows)

    def guidance(self) -> str | None:
        """The "Add now: ..." text for the current queue head, or None when nothing needs an Add."""
        from .ui import add_guidance

        q = self.queue()
        if q.head is None:
            return None
        by_id = {d.id: d for d in self.devices()}
        head = by_id.get(q.head)
        name = head.friendly_name if head and head.friendly_name else q.head
        return add_guidance(name, q.position or 1, q.total or 1)
