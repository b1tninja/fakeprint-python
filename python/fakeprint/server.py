"""The fakeprint WSD printer running inside this process (native binding), loopback only."""

from __future__ import annotations

import json
import re
import uuid as _uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx

from . import _paths
from ._native import Server as _NativeServer
from .client import ApiError, Capture, Client, Job, JobDocument, Status
from .manager import Manager
from .soap import PrintClient
from .ticket import PrintTicket

__all__ = [
    "Server",
    "Device",
    "DeviceHandle",
    "DEFAULT_DEVICE_ID",
    "Job",
    "JobDocument",
    "start",
    "FAST_TIMING",
    "TIMING_PRESETS",
]

DEFAULT_DEVICE_ID = "default"
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# The library prints a document for ~0.5 s + 0.5 s/MiB (jobs sit in Processing meanwhile). Tests
# want a job Completed by the time SendDocument returns, so the binding defaults to no printing
# time; pass ``timing={"processing_base_ms": 500, "processing_per_mib_ms": 500}`` for the daemon's.
FAST_TIMING = {"processing_base_ms": 0, "processing_per_mib_ms": 0}
# Presets of ``POST /v1/timing``: ``instant`` (no processing time), ``realistic`` (2 s + 1 s/MiB,
# 0.5 s terminating) and ``default`` (the library's own start-up timing).
TIMING_PRESETS = ("instant", "realistic", "default")


class _Controls:
    """Fault, injection, timing and level calls shared by :class:`Server` (its default device)
    and :class:`Device`; everything goes through ``self.control(method, path, body)``."""

    def control(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:  # pragma: no cover
        raise NotImplementedError

    # -- the library's fault plan (/v1/faults) -----------------------------------------------
    def faults(self, body: Mapping[str, Any] | None = None, **flags: Any) -> dict[str, Any]:
        """Switch printer faults and conditions, exactly as ``POST /v1/faults`` does.

        Flags: ``paper_out``, ``paper_low``, ``toner_low``, ``toner_empty``, ``cover_open``,
        ``paper_jam``, ``output_full``, ``error``, ``not_accepting_jobs``, ``temporary_error``,
        ``internal_error``, ``document_active`` (bools), ``fail_next_document`` (``Format`` /
        ``Compression`` / ``Transfer`` / ``None``), ``raise`` (``raise_=`` keyword) as a list of
        ``{"name", "component", "severity"}``, ``clear`` as a list of condition names, ``reset``.
        Without arguments it returns the current state: ``{"pending": ..., "active": ...}``.
        """
        payload = dict(body or {})
        for k, v in flags.items():
            payload["raise" if k == "raise_" else k] = v
        if not payload:
            status, doc = self.control("GET", "/v1/faults")
        else:
            status, doc = self.control("POST", "/v1/faults", payload)
        if status != 200:
            raise ValueError(f"/v1/faults: {status} {doc}")
        return doc  # type: ignore[no-any-return]

    def raise_condition(self, name: str, component: str | None = None, severity: str | None = None) -> dict[str, Any]:
        """Raise a device condition by ``ConditionNameType`` name (``CoverOpen``, ``Jam``, ...)."""
        spec: dict[str, Any] = {"name": name}
        if component:
            spec["component"] = component
        if severity:
            spec["severity"] = severity
        return self.faults(raise_=[spec])

    def clear_condition(self, *names: str) -> dict[str, Any]:
        return self.faults(clear=list(names))

    def clear_printer_faults(self) -> dict[str, Any]:
        """``DELETE /v1/faults``: switch off every condition and flag of the library's plan."""
        status, doc = self.control("DELETE", "/v1/faults")
        if status != 200:
            raise ValueError(f"DELETE /v1/faults: {status} {doc}")
        return doc  # type: ignore[no-any-return]

    # -- injected request faults and delays (server-side rules, /v1/faults "inject") --------
    def inject(self, rule: Mapping[str, Any] | None = None, **fields: Any) -> None:
        """Arm one server-side injection rule (see fakeprint ``docs/injection.md``).

        ``rule`` is the JSON object: ``op`` (short name or action URI; absent = any), ``endpoint``
        (``device``/``print_service``/``discovery``/``ipp``), ``skip``, ``count`` (``null`` =
        forever; absent = 1), ``delay_ms``, ``delay_after``, ``effect`` (``pass`` / ``fault`` /
        ``http`` / ``close_connection``). Keyword fields are merged in. ``ValueError`` when the
        server refuses the rule (HTTP 400); nothing is changed then.
        """
        body = {**(dict(rule) if rule else {}), **fields}
        status, doc = self.control("POST", "/v1/faults", {"inject": [body]})
        if status != 200:
            raise ValueError(f"inject refused: {status} {doc}")

    def inject_fault(
        self,
        action: str = "*",
        *,
        reason: str | None = None,
        code: str | None = None,
        subcode: str | None = None,
        http_status: int | None = None,
        delay_ms: int = 0,
        count: int | None = 1,
        skip: int = 0,
        endpoint: str | None = None,
        detail: str | None = None,
        body: str = "",
        content_type: str | None = None,
        close: bool = False,
    ) -> None:
        """Make the next ``count`` requests for ``action`` (short name, action URI or ``"*"``) fail.

        A SOAP 1.2 fault with ``code`` (``Sender``/``Receiver``) and an optional ``subcode`` QName
        such as ``"wprt:ClientErrorJobIdNotFound"``; with ``http_status`` alone, a bare HTTP answer
        (``body``/``content_type``); with ``close=True``, a dropped connection. ``skip`` lets that
        many matching requests through first; ``count=None`` stays armed until :meth:`clear_faults`.
        The rule lives in the server: every client sees it, and a faulted request never reaches
        the job engine. For faults the printer itself produces (paper out, ...) use :meth:`faults`.
        """
        if close:
            effect: dict[str, Any] = {"type": "close_connection"}
        elif http_status is not None and subcode is None and code is None and reason is None and detail is None:
            effect = {"type": "http", "status": http_status, "body": body}
            if content_type:
                effect["content_type"] = content_type
        else:
            if code is not None and code.lower() not in ("sender", "receiver"):
                raise ValueError("code must be Sender or Receiver")
            effect = {"type": "fault"}
            for k, v in (
                ("code", code and code.lower()),
                ("subcode", subcode),
                ("reason", reason),
                ("detail", detail),
                ("http_status", http_status),
            ):
                if v is not None:
                    effect[k] = v
        self._rule(action, effect, count=count, skip=skip, delay_ms=delay_ms, endpoint=endpoint)

    def inject_delay(
        self,
        action: str,
        delay_ms: int,
        count: int | None = None,
        *,
        skip: int = 0,
        endpoint: str | None = None,
        after: bool = False,
    ) -> None:
        """Hold replies to ``action`` for ``delay_ms`` (async, in the server) and process them
        normally: a slow printer. ``after=True`` processes first and sleeps before replying.
        ``count=None`` (default) stays armed."""
        self._rule(action, {"type": "pass"}, count=count, skip=skip, delay_ms=delay_ms, endpoint=endpoint,
                   delay_after=after)

    def _rule(
        self,
        action: str,
        effect: dict[str, Any],
        *,
        count: int | None,
        skip: int,
        delay_ms: int,
        endpoint: str | None,
        delay_after: bool = False,
    ) -> None:
        rule: dict[str, Any] = {"effect": effect, "count": count, "skip": skip}
        if action and action != "*":
            rule["op"] = action
        if endpoint:
            rule["endpoint"] = endpoint
        if delay_ms:
            rule["delay_ms"] = delay_ms
            if delay_after:
                rule["delay_after"] = True
        self.inject(rule)

    def clear_faults(self) -> None:
        """Disarm injection rules and switch off the printer's own fault plan (``DELETE /v1/faults``)."""
        self.clear_printer_faults()

    def pending_faults(self) -> list[tuple[str, int | None]]:
        """Armed injection rules as ``(op or "*", remaining)``; see :meth:`pending_rules`."""
        return [(r.get("op") or "*", r.get("remaining")) for r in self.pending_rules()]

    def pending_rules(self) -> list[dict[str, Any]]:
        """The rules of ``GET /v1/faults`` ``inject.pending`` (index, rule fields, skip_remaining,
        remaining, hits)."""
        return list(self.faults()["inject"]["pending"])

    def inject_stats(self) -> dict[str, int]:
        """``{"hits": effects applied, "skipped": requests let through by a skip}``."""
        inj = self.faults()["inject"]
        return {"hits": int(inj["hits"]), "skipped": int(inj["skipped"])}

    # -- job timing at run time (/v1/timing) --------------------------------------------------
    def timing(self, **patch: Any) -> dict[str, Any]:
        """Without arguments, ``GET /v1/timing``; with ``preset=`` and/or ``processing_base_ms``,
        ``processing_per_mib_ms``, ``document_timeout_ms``, ``terminating_ms``,
        ``document_busy_ms``, ``bytes_per_sheet``, ``POST /v1/timing`` (documents that arrive
        afterwards use the new values). Returns the resulting timing."""
        if not patch:
            status, doc = self.control("GET", "/v1/timing")
        else:
            status, doc = self.control("POST", "/v1/timing", patch)
        if status != 200:
            raise ValueError(f"/v1/timing: {status} {doc}")
        return doc  # type: ignore[no-any-return]

    def set_timing(self, preset: str | None = None, **patch: Any) -> dict[str, Any]:
        """``POST /v1/timing`` (``set_timing("realistic")``, ``set_timing(processing_base_ms=800)``)."""
        if preset is not None:
            patch["preset"] = preset
        return self.timing(**patch)

    # -- runtime levels (/v1/levels) ---------------------------------------------------------
    def set_levels(
        self, consumables: Mapping[str, int] | None = None, inputs: Mapping[str, int] | None = None, **named: int
    ) -> dict[str, Any]:
        """``POST /v1/levels``: set consumable levels (``{"Black": 15}``) and/or input-bin levels
        (``inputs={"Tray1": 0}``) at run time. Keyword arguments are consumables
        (``set_levels(Black=15)``). Never changes the published capabilities (so never makes an
        installed printer stale). Returns the ``live`` block."""
        cons = {**(dict(consumables) if consumables else {}), **named}
        body: dict[str, Any] = {}
        if cons:
            body["consumables"] = cons
        if inputs:
            body["inputs"] = dict(inputs)
        if not body:
            raise ValueError("set_levels needs consumables and/or inputs")
        status, doc = self.control("POST", "/v1/levels", body)
        if status != 200:
            raise ValueError(f"/v1/levels: {status} {doc}")
        return doc  # type: ignore[no-any-return]


class Device(_Controls):
    """One emulated printer: a description to start with, a live handle once a server runs it.

    >>> Server(devices=[Device("mono", profile="mono-laser"), Device("color", "color-inkjet-duplex")])  # doctest: +SKIP

    ``Device(id, profile="generic-wsd", device_uuid=None, friendly_name=None)`` is what you pass to
    ``Server(devices=[...])`` / ``Server.add_device``; ``None`` UUID/name mean "derive them" (the
    server derives a stable UUID from the id; the friendly name is ``fakeprint <id>``). The objects
    in ``srv.devices`` are bound to the running server and add the live API: identity (``uuid``,
    ``ipp_uuid``, ``profile``, ``friendly_name``, ``inbox``), the three endpoints, ``printer()`` /
    ``client()`` / ``http()``, ``jobs()`` / ``documents()`` / ``job_ticket()`` / ``captured()``,
    ``faults(...)`` / ``inject(...)`` / ``timing(...)`` / ``set_levels(...)``, ``set_profile()`` and
    ``reset()``. Two devices share nothing: jobs, faults, timing, levels and injection rules are
    per device. The first device is the *default* one, which also answers the legacy un-prefixed
    paths (``/v1/*``, ``/WebServices/*``, ``/ipp/print``); the others live under ``/d/<id>``.
    """

    def __init__(
        self,
        id: str,
        profile: str = "generic-wsd",
        device_uuid: str | None = None,
        friendly_name: str | None = None,
        *,
        jetdirect_port: int | None = None,
    ) -> None:
        if not DEVICE_ID_RE.match(id or ""):
            raise ValueError(f"device id {id!r}: use 1-32 characters of [A-Za-z0-9_-]")
        if device_uuid is not None:
            _uuid.UUID(device_uuid)  # ValueError when malformed
        self.id = id
        self._spec_profile = profile
        self.device_uuid_spec = device_uuid
        self._spec_name = friendly_name
        self.jetdirect_port = jetdirect_port
        self._server: Server | None = None
        self._info: dict[str, Any] = {}

    # -- the description ---------------------------------------------------------------------
    def spec(self) -> tuple[str, str, str | None, str | None]:
        """``(id, profile, uuid, friendly_name)``: what the native constructor takes."""
        return (self.id, self._spec_profile, self.device_uuid_spec, self._spec_name)

    @property
    def bound(self) -> bool:
        return self._server is not None

    def _bound_to(self, server: Server, info: Mapping[str, Any]) -> Device:
        """A new handle for the same description, bound to ``server`` (the spec stays reusable)."""
        d = Device(self.id, self._spec_profile, self.device_uuid_spec, self._spec_name, jetdirect_port=self.jetdirect_port)
        d._server = server
        d._info = dict(info)
        return d

    def _need(self) -> Server:
        if self._server is None:
            raise RuntimeError(f"Device {self.id!r} is not running; pass it to Server(devices=[...]) or Server.add_device")
        return self._server

    def _get(self, key: str, default: Any = None) -> Any:
        return self._info.get(key, default)

    def refresh(self) -> dict[str, Any]:
        """Re-read this device's ``DeviceInfo`` (``GET /v1/devices``); returns it."""
        info = self._need()._device_infos().get(self.id)
        if info is not None:
            self._info = dict(info)
        return dict(self._info)

    @property
    def info(self) -> dict[str, Any]:
        """The ``DeviceInfo`` row as last read (``refresh()`` re-reads it; ``live`` changes with state)."""
        return dict(self._info)

    def live(self) -> dict[str, Any]:
        """A fresh ``live`` block (printer state, faults, job counts, levels, timing preset)."""
        return dict(self.refresh().get("live") or {})

    # -- identity ----------------------------------------------------------------------------
    @property
    def uuid(self) -> str | None:
        return self._get("uuid") or self.device_uuid_spec

    @property
    def ipp_uuid(self) -> str | None:
        return self._get("ipp_uuid")

    @property
    def profile(self) -> str:
        return str(self._get("profile") or self._spec_profile)

    @property
    def friendly_name(self) -> str | None:
        return self._get("friendly_name") or self._spec_name

    @property
    def is_default(self) -> bool:
        return self._server is not None and self.id == self._server._default_id

    @property
    def base_path(self) -> str:
        """``""`` for the default device, ``/d/<id>`` for the others."""
        return "" if self._server is None or self.is_default else f"/d/{self.id}"

    @property
    def inbox(self) -> Path | None:
        d = self._get("inbox_dir")
        return Path(d) if d else None

    inbox_dir = inbox
    service_endpoint = property(lambda self: self._get("service_endpoint"))
    device_endpoint = property(lambda self: self._get("device_endpoint"))
    ipp_endpoint = property(lambda self: self._get("ipp_endpoint"))

    @property
    def needs_readd(self) -> bool:
        return bool(self._get("needs_readd", False))

    @property
    def class_(self) -> str | None:
        """The capability class handle (first 12 hex digits of the published fingerprint)."""
        return (self._get("config") or {}).get("class")

    # -- clients -----------------------------------------------------------------------------
    def control(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """Call this device's control plane: ``control("GET", "/v1/status")`` reaches
        ``/d/<id>/v1/status`` (the default device: in-process like :meth:`Server.control`)."""
        return self._need()._device_control(self, method, path, body)

    def client(self, **kwargs: Any) -> Client:
        """A control :class:`~fakeprint.client.Client` scoped to this device (``/d/<id>/v1/...``)."""
        srv = self._need()
        c = Client(srv.base_url, device=None if self.is_default else self.id, **kwargs)
        srv._clients.append(c)
        return c

    def http(self, *, timeout: float = 15.0, **kwargs: Any) -> httpx.Client:
        """A plain ``httpx.Client`` (closed by ``Server.stop()``)."""
        return self._need().http(timeout=timeout, **kwargs)

    def printer(self, *, timeout: float = 15.0) -> PrintClient:
        """A WS-Print :class:`~fakeprint.soap.PrintClient` bound to this device's PrinterService."""
        self._need()
        ep = self.service_endpoint
        if not ep:
            raise RuntimeError(f"device {self.id!r} has no WSD endpoint (wsd is off)")
        return PrintClient(ep, client=self.http(timeout=timeout))

    def device_client(self, *, timeout: float = 15.0) -> PrintClient:
        """A :class:`~fakeprint.soap.PrintClient` on this device's metadata (Device) endpoint."""
        self._need()
        ep = self.device_endpoint
        if not ep:
            raise RuntimeError(f"device {self.id!r} has no WSD endpoint (wsd is off)")
        return PrintClient(ep, client=self.http(timeout=timeout))

    # -- state -------------------------------------------------------------------------------
    def _ok(self, method: str, path: str, body: Any = None) -> Any:
        status, doc = self.control(method, path, body)
        if status != 200:
            raise ValueError(f"{self.id}: {method} {path}: {status} {doc}")
        return doc

    def status(self) -> Status:
        return Status(self._ok("GET", "/v1/status"))

    def _jobs_doc(self) -> dict[str, Any]:
        doc = self._ok("GET", "/v1/jobs")
        return doc if isinstance(doc, dict) else {}

    def captured(self) -> list[Capture]:
        """This device's captured documents, oldest first."""
        return [Capture.from_json(d) for d in self._jobs_doc().get("jobs", [])]

    def jobs(self) -> list[Job]:
        """Every job this printer remembers (history included), ascending by id."""
        return [Job.from_json(d) for d in self._jobs_doc().get("print_jobs", [])]

    def job(self, job_id: int) -> Job | None:
        return next((j for j in self.jobs() if j.job_id == job_id), None)

    def job_ticket(self, job_id: int) -> PrintTicket:
        """The print ticket this printer stored for ``job_id`` (``GetJobElements(PrintTicket)``)."""
        with self.printer() as pc:
            return pc.get_job_ticket(job_id)

    def documents(self, job_id: int) -> list[bytes]:
        """The bytes of each document captured for ``job_id`` on THIS device, in document order."""
        rows = sorted((c for c in self.captured() if c.job_id == job_id), key=lambda c: c.document_id)
        return [Path(c.path).read_bytes() for c in rows]

    def device_cancel(self, job_id: int) -> bool:
        """Cancel a job as if from this printer's panel. True when it was active."""
        return self._need()._device_cancel(self, job_id)

    def reset(self) -> None:
        """Forget this device's jobs, captures, counters and conditions; disarm its injection rules.
        Install state and other devices are untouched."""
        self._ok("POST", "/v1/reset")
        self.refresh()

    def set_profile(self, profile_id: str) -> Status:
        """Hot-swap this device's capability profile (``KeyError`` when it cannot be loaded). The
        published capabilities may change, which makes an installed printer ``stale`` (re-Add)."""
        status, doc = self.control("POST", "/v1/profile", {"id": profile_id})
        if status == 404 or (status == 400 and "profile" in str(doc).lower()):
            raise KeyError(f"{self.id}: {doc}")
        if status != 200:
            raise ValueError(f"{self.id}: set_profile {profile_id!r}: {status} {doc}")
        self.refresh()
        return Status(doc)

    def set_profile_doc(self, profile: Mapping[str, Any]) -> Status:
        status, doc = self.control("POST", "/v1/profile", {"profile": dict(profile)})
        if status != 200:
            raise ValueError(f"{self.id}: set_profile_doc: {status} {doc}")
        self.refresh()
        return Status(doc)

    def validation_problems(self) -> list[str]:
        """Schema violations this device found in its own outgoing messages (the served recent list)."""
        return [str(x) for x in (self.status().validation.get("recent") or [])]

    def validation_problem_count(self) -> int:
        return int(self.status().validation.get("problems") or 0)

    def __repr__(self) -> str:
        if self._server is None:
            return f"Device({self.id!r}, profile={self._spec_profile!r})"
        return f"Device({self.id!r}, profile={self.profile!r}, uuid={self.uuid})"


DeviceHandle = Device  # the objects of ``Server.devices``


class Server(_Controls):
    """A fake WS-Print printer on ``127.0.0.1:<ephemeral port>``.

    >>> with Server() as srv:                                   # doctest: +SKIP
    ...     job = srv.printer().print_document(b"%PDF-1.4", format="application/pdf")
    ...     srv.documents(job)
    [b'%PDF-1.4']

    The server is the Rust library's own (``fakeprint::server::serve`` with ``ServerConfig::embedded``):
    same routes, job engine, events and ``/v1`` control plane as the daemon, with WS-Discovery, the
    JetDirect :9100 listener and the Windows named pipe never started, so an embedded server cannot
    be seen by (or collide with) anything outside this process. Captured documents land in a
    private temp directory (``inbox``) that ``stop()`` removes unless you passed your own.

    ``limits`` and ``timing`` are dicts of overrides: ``max_document``, ``max_body``,
    ``max_documents_per_job``, ``max_active_jobs``, ``history_cap``, ``max_name``,
    ``max_requested_names``, ``max_condition_history``; and ``document_timeout_ms``,
    ``terminating_ms``, ``processing_base_ms``, ``processing_per_mib_ms``, ``document_busy_ms``,
    ``bytes_per_sheet``; ``timing`` defaults to :data:`FAST_TIMING` (jobs complete at once). ``timing``
    may also be a preset name (``"instant"``, ``"realistic"``, ``"default"``) or a dict with a
    ``"preset"`` key plus overrides; those go through ``POST /v1/timing`` right after start-up.
    :meth:`timing` / :meth:`set_timing` change it at run time (documents arriving afterwards).
    Request faults and delays (:meth:`inject`, :meth:`inject_fault`, :meth:`inject_delay`) are
    rules in the *server*, so they affect every client, pdfcraft's included.
    ``validate_replies`` (default on) checks every outgoing message against the
    WS-Print schema; see :meth:`validation_problems`.

    **Several printers.** ``Server(devices=[Device("mono", "mono-laser"), Device("color", "...")])``
    runs N printers behind one port; ``srv.devices`` maps id to :class:`Device` (jobs, faults,
    timing, levels and injection rules are per device). The first device is the default one:
    ``srv.printer()`` / ``srv.client()`` / every ``Server`` method address it, on the legacy paths.
    Without ``devices`` there is exactly one device named ``default`` (``profile``, ``device_uuid``,
    ``ipp_uuid`` and ``name`` configure it; they are not allowed together with ``devices``).
    ``srv.manager`` is a :class:`~fakeprint.manager.Manager` bound to this server.
    """

    def __init__(
        self,
        *,
        devices: Sequence[Device] | None = None,
        profile: str = "generic-wsd",
        profiles: str | Path | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        device_uuid: str | None = None,
        ipp_uuid: str | None = None,
        inbox: str | Path | None = None,
        name: str | None = None,
        wsd: bool = True,
        ipp: bool = True,
        validate_replies: bool = True,
        limits: Mapping[str, int] | None = None,
        timing: str | Mapping[str, Any] | None = None,
        initial_events: bool = False,
        event_rate_secs: int | None = None,
        tick_ms: int | None = None,
    ) -> None:
        specs: list[Device] = list(devices or [])
        if specs:
            if device_uuid is not None or ipp_uuid is not None or name is not None:
                raise ValueError(
                    "device_uuid/ipp_uuid/name configure the implicit default device; set them on a Device instead"
                )
            ids = [d.id for d in specs]
            if len(set(ids)) != len(ids):
                raise ValueError(f"duplicate device ids in {ids}")
        native_timing: dict[str, int] = dict(FAST_TIMING)
        late_timing: dict[str, Any] | None = None
        if isinstance(timing, str):
            timing = {"preset": timing}
        if timing:
            t = dict(timing)
            if "preset" in t:
                native_timing = {}
                late_timing = t
            else:
                native_timing.update(t)
        extra: dict[str, Any] = {}
        if specs:
            extra["devices"] = [d.spec() for d in specs]
        try:
            self._native = _NativeServer(
                profiles=str(profiles if profiles is not None else _paths.profiles_dir()),
                profile=profile,
                host=host,
                port=port,
                device_uuid=device_uuid,
                ipp_uuid=ipp_uuid,
                inbox=None if inbox is None else str(inbox),
                name=name,
                wsd=wsd,
                ipp=ipp,
                validate_replies=validate_replies,
                limits=dict(limits) if limits else None,
                timing=native_timing,
                initial_events=initial_events,
                event_rate_secs=event_rate_secs,
                tick_ms=tick_ms,
                **extra,
            )
        except TypeError as e:
            if extra:
                raise RuntimeError(
                    "this fakeprint native module has no multi-device support; rebuild it (maturin develop) "
                    f"against a fakeprint with multi-device `serve`: {e}"
                ) from e
            raise
        self._clients: list[Any] = []
        self._ipp_on = ipp
        self._manager: Manager | None = None
        self._ctl_http: httpx.Client | None = None
        self.devices: dict[str, Device] = {}
        try:
            self._default_id = specs[0].id if specs else DEFAULT_DEVICE_ID
            self._load_devices(specs)
        except Exception:
            self._native.stop()
            raise
        if late_timing is not None:
            try:
                self.set_timing(**late_timing)
            except Exception:
                self._native.stop()
                raise

    # -- identity ----------------------------------------------------------------------------
    port = property(lambda self: self._native.port)
    base_url = property(lambda self: self._native.base_url)
    running = property(lambda self: self._native.running)
    device_uuid = property(lambda self: self._native.device_uuid)
    ipp_uuid = property(lambda self: self._native.ipp_uuid)
    inbox_dir = property(lambda self: Path(self._native.inbox_dir))
    device_endpoint = property(lambda self: self._native.device_endpoint)
    service_endpoint = property(lambda self: self._native.service_endpoint)

    # -- devices -----------------------------------------------------------------------------
    def _device_infos(self) -> dict[str, dict[str, Any]]:
        """``GET /v1/devices`` as ``{id: DeviceInfo}``; falls back to the native view (and for a
        library without the route, to the one default device) so a single-device server never
        depends on the manager routes."""
        status, doc = self.control("GET", "/v1/devices")
        if status == 200 and isinstance(doc, list) and doc:
            return {str(r["id"]): dict(r) for r in doc if isinstance(r, dict) and "id" in r}
        native = getattr(self._native, "devices", None)
        if native:
            return {
                str(i): {
                    "id": str(i), "uuid": u, "ipp_uuid": iu, "profile": p, "friendly_name": fn, "inbox_dir": ib,
                    "device_endpoint": self._endpoint_for(i, "device"),
                    "service_endpoint": self._endpoint_for(i, "service"),
                    "ipp_endpoint": None,
                    "default": i == self._default_id,
                    "base_path": "" if i == self._default_id else f"/d/{i}",
                }
                for i, u, iu, p, fn, ib in native
            }
        return {
            self._default_id: {
                "id": self._default_id, "default": True, "base_path": "",
                "uuid": self._native.device_uuid, "ipp_uuid": self._native.ipp_uuid,
                "profile": self.status().profile_id, "friendly_name": self.status().name,
                "inbox_dir": self._native.inbox_dir,
                "device_endpoint": self._native.device_endpoint,
                "service_endpoint": self._native.service_endpoint,
                "ipp_endpoint": self._ipp_default(),
            }
        }

    def _ipp_default(self) -> str | None:
        return f"ipp://127.0.0.1:{self.port}/ipp/print" if self._ipp_on else None

    def _endpoint_for(self, dev_id: str, which: str) -> str:
        pre = "" if dev_id == self._default_id else f"/d/{dev_id}"
        tail = "/WebServices/Device" if which == "device" else "/WebServices/PrinterService"
        return f"{self.base_url}{pre}{tail}"

    def _load_devices(self, specs: Sequence[Device]) -> None:
        infos = self._device_infos()
        by_spec = {d.id: d for d in specs}
        order = [d.id for d in specs] or list(infos)
        # The first device is the default one; the library flags it too.
        flagged = next((i for i, r in infos.items() if r.get("default")), None)
        self._default_id = flagged or (order[0] if order else DEFAULT_DEVICE_ID)
        self.devices.clear()
        for dev_id in order:
            info = infos.get(dev_id)
            if info is None:
                raise RuntimeError(f"the server did not report device {dev_id!r}; it reported {list(infos)}")
            self.devices[dev_id] = (by_spec.get(dev_id) or Device(dev_id, info.get("profile", "generic-wsd")))._bound_to(self, info)

    @property
    def default_device(self) -> Device:
        return self.devices[self._default_id]

    def _device_control(self, dev: Device, method: str, path: str, body: Any) -> tuple[int, Any]:
        if dev.is_default:
            return self.control(method, path, body)
        # Non-default devices are addressed under /d/<id> over loopback HTTP.
        if self._ctl_http is None:
            self._ctl_http = httpx.Client(base_url=self.base_url, timeout=15.0)
        r = self._ctl_http.request(
            method, dev.base_path + path, content=None if body is None else json.dumps(body),
            headers={"content-type": "application/json"} if body is not None else None,
        )
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text

    def _device_cancel(self, dev: Device, job_id: int) -> bool:
        if dev.is_default:
            return self.device_cancel(job_id)
        try:
            return bool(self._native.device_cancel(job_id, device=dev.id))
        except TypeError as e:
            raise RuntimeError("this native module cannot cancel on a non-default device; rebuild it") from e

    @property
    def manager(self) -> Manager:
        """A :class:`~fakeprint.manager.Manager` for this server (created lazily; closed by ``stop()``).
        An embedded server keeps install state in memory only: no registry file, no discovery."""
        if self._manager is None:
            self._manager = Manager(self.base_url)
        return self._manager

    def open_ui(self, opener: Callable[[str], Any] | None = None) -> str:
        """Open this server's ``/ui`` page in a browser (needs a fakeprint build with the route);
        returns the URL. ``opener`` replaces ``webbrowser.open`` (tests)."""
        return self.manager.open_ui(opener)

    def add_device(self, device: Device) -> Device:
        """Add a printer while the server runs (``POST /v1/devices``). A duplicate id/UUID, a bad
        profile or a bad name raise :class:`~fakeprint.manager.Conflict` / ``BadRequest`` (both
        :class:`~fakeprint.client.ApiError`); until the library supports it, ``NotYet`` (501)."""
        info = self.manager.add_info(
            device.id, device.spec()[1], device_uuid=device.device_uuid_spec,
            friendly_name=device.spec()[3], jetdirect_port=device.jetdirect_port,
        )
        handle = device._bound_to(self, info)
        self.devices[handle.id] = handle
        return handle

    def remove_device(self, id: str) -> None:
        """Stop and remove a printer while the server runs; the other devices are untouched."""
        if id == self._default_id and len(self.devices) > 1:
            raise ValueError("the default (first) device cannot be removed while others run")
        self.manager.remove(id, forget=True)
        self.devices.pop(id, None)

    # -- clients -----------------------------------------------------------------------------
    def client(self, **kwargs: Any) -> Client:
        """A control-plane :class:`~fakeprint.client.Client` over HTTP (closed by ``stop()``)."""
        c = Client(self.base_url, **kwargs)
        self._clients.append(c)
        return c

    def http(self, *, timeout: float = 15.0, **kwargs: Any) -> httpx.Client:
        """A plain ``httpx.Client`` (closed by ``stop()``); injected faults are server-side."""
        c = httpx.Client(timeout=timeout, **kwargs)
        self._clients.append(c)
        return c

    def printer(self, *, timeout: float = 15.0, endpoint: str | None = None) -> PrintClient:
        """A WS-Print :class:`~fakeprint.soap.PrintClient` on the PrinterService endpoint (or
        ``endpoint``). Server-side rules (:meth:`inject_fault` ...) reach it like any other client."""
        return PrintClient(endpoint or self.service_endpoint, client=self.http(timeout=timeout))

    def device(self, *, timeout: float = 15.0) -> PrintClient:
        """A :class:`~fakeprint.soap.PrintClient` on the Device (metadata) endpoint."""
        return self.printer(timeout=timeout, endpoint=self.device_endpoint)

    # -- state -------------------------------------------------------------------------------
    def status(self) -> Status:
        return Status(json.loads(self._native.status_json()))

    def profiles(self) -> list[str]:
        """Profile ids this server can switch to (``set_profile``)."""
        return self.status().available_profiles

    def captured(self) -> list[Capture]:
        """Every captured document, oldest first (the inbox)."""
        return [Capture.from_json(d) for d in json.loads(self._native.captured_json())]

    def print_jobs_json(self) -> str:
        """The ``print_jobs`` rows of ``/v1/jobs`` as JSON text."""
        return str(self._native.print_jobs_json())

    def jobs_json(self) -> str:
        return self.print_jobs_json()

    def jobs(self) -> list[Job]:
        """Every job the printer remembers, history included, ascending by id."""
        return [Job.from_json(d) for d in json.loads(self._native.print_jobs_json())]

    def job(self, job_id: int) -> Job | None:
        return next((j for j in self.jobs() if j.job_id == job_id), None)

    def job_ticket(self, job_id: int) -> PrintTicket:
        """The print ticket the printer stored for ``job_id`` (``GetJobElements(PrintTicket)`` over
        loopback; the ``/v1/jobs`` rows do not carry it, see docs/needs-from-rust.md)."""
        with PrintClient(self.service_endpoint) as pc:
            return pc.get_job_ticket(job_id)

    def documents(self, job_id: int) -> list[bytes]:
        """The bytes of each document captured for ``job_id``, in document order."""
        return list(self._native.documents(job_id))

    def device_cancel(self, job_id: int) -> bool:
        """Cancel a job as if from the printer's panel. True when it was active."""
        return bool(self._native.device_cancel(job_id))

    def reset(self) -> None:
        """Forget jobs, captures, counters and conditions; disarm injected faults. Inbox files stay."""
        self._native.reset()
        self.control("POST", "/v1/reset")  # also drops injection rules

    def set_profile(self, profile_id: str) -> None:
        """Hot-swap the capability profile by id (``KeyError`` when it cannot be loaded)."""
        self._native.set_profile(profile_id)

    def set_profile_doc(self, profile: Mapping[str, Any]) -> None:
        """Install a schema-2 profile document (a dict) directly (``ValueError`` when invalid)."""
        self._native.set_profile_json(json.dumps(dict(profile)))

    def control(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """Call a library control route in-process (no HTTP): ``(status, parsed JSON or text)``."""
        status, text = self._native.control(method, path, None if body is None else json.dumps(body))
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    # -- reply validation --------------------------------------------------------------------
    def validation_problems(self) -> list[str]:
        """Schema violations the server found in its own outgoing messages (newest 64)."""
        return list(self._native.validation_problems())

    def validation_problem_count(self) -> int:
        return int(self._native.validation_problem_count())

    # -- lifecycle ---------------------------------------------------------------------------
    def stop(self) -> None:
        if self._manager is not None:
            self._manager.close()
            self._manager = None
        if self._ctl_http is not None:
            self._ctl_http.close()
            self._ctl_http = None
        for c in self._clients:
            try:
                c.close()
            except Exception:
                pass
        self._clients.clear()
        self._native.stop()

    def __enter__(self) -> Server:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __repr__(self) -> str:
        return f"Server({self.base_url}, profile={self.status().profile_id!r})"


def start(profile: str = "generic-wsd", **kwargs: Any) -> Server:
    """Start an embedded server on any shipped profile (``fakeprint.profiles()`` lists them)."""
    return Server(profile=profile, **kwargs)
