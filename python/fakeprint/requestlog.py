"""The raw request log of a fakeprint device (``/v1/requests``): typed records and the calls.

Every device keeps a bounded ring of the exact requests its wire endpoints received (WS-Print SOAP,
IPP, directed discovery) and the event deliveries it sent, with the HTTP headers as the client wrote
them, the bodies (document payloads as a head slice plus length and SHA-256), a parsed summary, the
reply, the injection rule that applied and the ticket substitutions the validator made. See
fakeprint's ``docs/request-log.md``.

:class:`Request` is one record; :class:`Body` a stored (possibly truncated) body;
:class:`TicketSubstitution` one soft value the printer replaced or dropped. The mixin
:class:`RequestLogCalls` gives :class:`~fakeprint.client.Client`, :class:`~fakeprint.server.Server`
and :class:`~fakeprint.server.Device` the same four methods: :meth:`requests`,
:meth:`request_raw`, :meth:`clear_requests` and :meth:`request_log_config`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Body", "Request", "RequestPage", "TicketSubstitution", "RequestLogCalls"]


@dataclass(frozen=True)
class Body:
    """A stored body. ``data`` is at most ``stored`` bytes; ``length`` is the real size on the wire."""

    length: int
    stored: int
    truncated: bool
    document: bool = False
    sha256: str | None = None
    data: bytes = b""
    text: str | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any] | None) -> Body:
        d = d or {}
        b64 = d.get("base64")
        return cls(
            length=int(d.get("length", 0)),
            stored=int(d.get("stored", 0)),
            truncated=bool(d.get("truncated", False)),
            document=bool(d.get("document", False)),
            sha256=d.get("sha256"),
            data=base64.b64decode(b64) if b64 else b"",
            text=d.get("text"),
        )

    @property
    def complete(self) -> bool:
        """True when ``data`` is the whole body."""
        return not self.truncated


@dataclass(frozen=True)
class TicketSubstitution:
    """One thing the ticket validator (WS-Print) or the attribute check (IPP) changed."""

    field: str
    requested: str
    #: ``None``: the element / attribute was dropped.
    applied: str | None
    reason: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> TicketSubstitution:
        return cls(str(d["field"]), str(d.get("requested", "")), d.get("applied"), str(d.get("reason", "")))


@dataclass(frozen=True)
class Request:
    """One record of the request log (``GET /v1/requests`` ``records[]``)."""

    seq: int
    #: ``"in"`` (a request the device received) or ``"out"`` (an event delivery it sent).
    direction: str
    t_mono_ms: float
    t_wall_unix_ms: int
    t_wall: str
    peer: str | None
    method: str
    path: str
    query: str | None
    version: str
    protocol: str
    endpoint: str | None
    action: str | None
    action_uri: str | None
    message_id: str | None
    request_id: int | None
    ipp_operation: str | None
    ipp_operation_id: int | None
    ipp_status: str | None
    job_id: int | None
    latency_us: int
    #: ``<op>[<index>]:<effect>`` of the injection rule that applied, else ``None``.
    fault_rule: str | None
    #: ``"ok"``, ``"faulted"`` (a fault / error reply) or ``"dropped"`` (no reply reached the client).
    result: str
    #: Request headers in wire order; case as sent when :attr:`headers_raw`.
    headers: list[tuple[str, str]]
    headers_raw: bool
    body: Body
    status: int | None
    response_headers: list[tuple[str, str]]
    response_body: Body
    #: The ``PrintTicket`` element text exactly as received (WS-Print ``CreatePrintJob``), else ``None``.
    ticket_received: str | None = None
    substitutions: list[TicketSubstitution] = field(default_factory=list)
    #: ``{"version", "request_attrs": [...], "response_attrs": [...], "document_bytes"}`` for IPP.
    ipp: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Request:
        req = d.get("request") or {}
        resp = d.get("response") or {}
        ticket = d.get("ticket") or {}
        return cls(
            seq=int(d["seq"]),
            direction=str(d.get("direction", "in")),
            t_mono_ms=float(d.get("t_mono_ms", 0.0)),
            t_wall_unix_ms=int(d.get("t_wall_unix_ms", 0)),
            t_wall=str(d.get("t_wall", "")),
            peer=d.get("peer"),
            method=str(d.get("method", "")),
            path=str(d.get("path", "")),
            query=d.get("query"),
            version=str(d.get("version", "")),
            protocol=str(d.get("protocol", "")),
            endpoint=d.get("endpoint"),
            action=d.get("action"),
            action_uri=d.get("action_uri"),
            message_id=d.get("message_id"),
            request_id=d.get("request_id"),
            ipp_operation=d.get("ipp_operation"),
            ipp_operation_id=d.get("ipp_operation_id"),
            ipp_status=d.get("ipp_status"),
            job_id=d.get("job_id"),
            latency_us=int(d.get("latency_us", 0)),
            fault_rule=d.get("fault_rule"),
            result=str(d.get("result", "ok")),
            headers=[(str(k), str(v)) for k, v in req.get("headers", [])],
            headers_raw=bool(req.get("headers_raw", False)),
            body=Body.from_json(req.get("body")),
            status=resp.get("status"),
            response_headers=[(str(k), str(v)) for k, v in resp.get("headers", [])],
            response_body=Body.from_json(resp.get("body")),
            ticket_received=ticket.get("received"),
            substitutions=[TicketSubstitution.from_json(s) for s in ticket.get("substitutions", [])],
            ipp=d.get("ipp"),
        )

    # -- headers -----------------------------------------------------------------------------
    def header(self, name: str) -> str | None:
        """The first request header called ``name`` (case-insensitive), else ``None``."""
        n = name.lower()
        return next((v for k, v in self.headers if k.lower() == n), None)

    def headers_named(self, name: str) -> list[str]:
        """Every value of the request header ``name`` (duplicates stay separate)."""
        n = name.lower()
        return [v for k, v in self.headers if k.lower() == n]

    def header_names(self) -> list[str]:
        """The request header names in wire order, as sent when :attr:`headers_raw`."""
        return [k for k, _ in self.headers]

    @property
    def user_agent(self) -> str | None:
        return self.header("user-agent")

    @property
    def content_type(self) -> str | None:
        return self.header("content-type")

    @property
    def chunked(self) -> bool:
        """The request used ``Transfer-Encoding: chunked``."""
        return "chunked" in (self.header("transfer-encoding") or "").lower()

    @property
    def expects_continue(self) -> bool:
        return (self.header("expect") or "").lower().startswith("100")

    @property
    def is_mtom(self) -> bool:
        """The body is a ``multipart/related`` (MTOM / XOP) package."""
        return (self.content_type or "").lower().startswith("multipart/related")

    @property
    def soap_action(self) -> str | None:
        return self.header("soapaction")

    # -- ticket ------------------------------------------------------------------------------
    @property
    def has_document_processing(self) -> bool:
        """The received ticket carries a ``DocumentProcessing`` element (WS-Print)."""
        return bool(self.ticket_received) and "DocumentProcessing" in (self.ticket_received or "")

    @property
    def has_job_processing(self) -> bool:
        return bool(self.ticket_received) and "JobProcessing" in (self.ticket_received or "")

    def substituted(self, field_name: str) -> TicketSubstitution | None:
        return next((s for s in self.substitutions if s.field.lower() == field_name.lower()), None)

    @property
    def faulted(self) -> bool:
        return self.result == "faulted"

    @property
    def dropped(self) -> bool:
        return self.result == "dropped"


@dataclass(frozen=True)
class RequestPage:
    """``GET /v1/requests`` with its bookkeeping."""

    records: list[Request]
    count: int
    dropped: int
    next_seq: int
    in_flight: int
    stored_bytes: int
    config: dict[str, Any]

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> RequestPage:
        return cls(
            records=[Request.from_json(r) for r in d.get("records", [])],
            count=int(d.get("count", 0)),
            dropped=int(d.get("dropped", 0)),
            next_seq=int(d.get("next_seq", 1)),
            in_flight=int(d.get("in_flight", 0)),
            stored_bytes=int(d.get("stored_bytes", 0)),
            config=dict(d.get("config", {})),
        )


def _query(
    since: int | None, limit: int | None, action: str | None, job: int | None, direction: str | None, bodies: bool
) -> str:
    from urllib.parse import quote

    parts: list[str] = []
    if since is not None:
        parts.append(f"since={int(since)}")
    if limit is not None:
        parts.append(f"limit={int(limit)}")
    if action:
        parts.append(f"action={quote(action, safe='')}")
    if job is not None:
        parts.append(f"job={int(job)}")
    if direction:
        parts.append(f"direction={quote(direction, safe='')}")
    if not bodies:
        parts.append("bodies=0")
    return ("?" + "&".join(parts)) if parts else ""


class RequestLogCalls:
    """The request-log calls, shared by ``Client``, ``Server`` and ``Device``.

    A host provides ``_log_call(device, method, path, body)`` returning ``(status, parsed JSON or
    text)``; the ``device`` argument selects another device of a multi-device server (the
    default device when ``None``)."""

    def _log_call(self, device: str | None, method: str, path: str, body: Any = None) -> tuple[int, Any]:  # pragma: no cover
        raise NotImplementedError

    def _log_ok(self, device: str | None, method: str, path: str, body: Any = None) -> Any:
        status, doc = self._log_call(device, method, path, body)
        if status != 200:
            raise ValueError(f"{method} {path}: {status} {doc}")
        return doc

    def requests_page(
        self,
        device: str | None = None,
        *,
        since: int | None = None,
        limit: int | None = None,
        action: str | None = None,
        job: int | None = None,
        direction: str | None = None,
        bodies: bool = True,
    ) -> RequestPage:
        """``GET /v1/requests`` with the ring's bookkeeping (``dropped``, ``next_seq``, ``config``)."""
        doc = self._log_ok(device, "GET", "/v1/requests" + _query(since, limit, action, job, direction, bodies))
        return RequestPage.from_json(doc)

    def requests(
        self,
        device: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        action: str | None = None,
        job: int | None = None,
        *,
        direction: str | None = None,
        bodies: bool = True,
    ) -> list[Request]:
        """The logged requests, oldest first: those with ``seq > since``, at most ``limit`` (server
        default 100), only ``action`` (a WSD short action such as ``"CreatePrintJob"``, a full action
        URI or an IPP operation such as ``"Print-Job"``, case-insensitive) and only ``job``. Poll
        with ``since=<last seq>``; ``direction="out"`` selects the event deliveries the device sent."""
        return self.requests_page(
            device, since=since, limit=limit, action=action, job=job, direction=direction, bodies=bodies
        ).records

    def request(self, seq: int, device: str | None = None) -> Request | None:
        """One record by ``seq``; ``None`` when it never existed or the ring already dropped it."""
        status, doc = self._log_call(device, "GET", f"/v1/requests/{int(seq)}")
        if status == 404:
            return None
        if status != 200:
            raise ValueError(f"GET /v1/requests/{seq}: {status} {doc}")
        return Request.from_json(doc)

    def request_raw(
        self, seq: int, device: str | None = None, *, part: str = "request", body_only: bool = False
    ) -> bytes | None:
        """The raw bytes of a record: the request (wire head plus stored body) or, with
        ``part="response"``, the reply (rebuilt head plus stored body); ``body_only`` returns only the
        stored body bytes. ``None`` when the record is gone."""
        if part not in ("request", "response"):
            raise ValueError("part must be 'request' or 'response'")
        what = "body" if body_only else "all"
        status, doc = self._log_call(
            device, "GET", f"/v1/requests/{int(seq)}/raw?part={part}&what={what}&encoding=base64"
        )
        if status == 404:
            return None
        if status != 200:
            raise ValueError(f"GET /v1/requests/{seq}/raw: {status} {doc}")
        return base64.b64decode(doc["base64"])

    def clear_requests(self, device: str | None = None) -> int:
        """``DELETE /v1/requests``: drop every record (seq numbers keep counting). Returns how many."""
        doc = self._log_ok(device, "DELETE", "/v1/requests")
        return int(doc.get("cleared", 0))

    def request_log_config(self, device: str | None = None, **patch: Any) -> dict[str, Any]:
        """Read (no arguments) or patch the ring: ``enabled``, ``capacity`` (0 = off), ``body_cap``,
        ``doc_head``, ``max_bytes``, ``redact``. Returns the resulting configuration."""
        if not patch:
            return dict(self._log_ok(device, "GET", "/v1/requests/config"))
        return dict(self._log_ok(device, "POST", "/v1/requests/config", patch))
