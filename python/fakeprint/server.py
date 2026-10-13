"""The fakeprint WSD printer running inside this process (native binding), loopback only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import httpx

from . import _paths
from ._native import Server as _NativeServer
from .client import Capture, Client, Job, JobDocument, Status
from .soap import PrintClient
from .ticket import PrintTicket

__all__ = ["Server", "Job", "JobDocument", "start", "FAST_TIMING", "TIMING_PRESETS"]

# The library prints a document for ~0.5 s + 0.5 s/MiB (jobs sit in Processing meanwhile). Tests
# want a job Completed by the time SendDocument returns, so the binding defaults to no printing
# time; pass ``timing={"processing_base_ms": 500, "processing_per_mib_ms": 500}`` for the daemon's.
FAST_TIMING = {"processing_base_ms": 0, "processing_per_mib_ms": 0}
# Presets of ``POST /v1/timing``: ``instant`` (no processing time), ``realistic`` (2 s + 1 s/MiB,
# 0.5 s terminating) and ``default`` (the library's own start-up timing).
TIMING_PRESETS = ("instant", "realistic", "default")


class Server:
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
    """

    def __init__(
        self,
        *,
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
        )
        self._clients: list[Any] = []
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

    # -- reply validation --------------------------------------------------------------------
    def validation_problems(self) -> list[str]:
        """Schema violations the server found in its own outgoing messages (newest 64)."""
        return list(self._native.validation_problems())

    def validation_problem_count(self) -> int:
        return int(self._native.validation_problem_count())

    # -- lifecycle ---------------------------------------------------------------------------
    def stop(self) -> None:
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
