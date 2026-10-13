"""A WS-Print SOAP client for tests: build requests, parse replies, turn faults into exceptions.

Written from the operation and fault notes in the emulator repo (docs/reference/operations.md,
faults.md); it carries no Microsoft schema text. It talks to whatever URL it is given, so point
it at an embedded :class:`fakeprint.Server` (or another loopback emulator), never at a real
printer from a test.

>>> pc = PrintClient("http://127.0.0.1:3921/WebServices/PrinterService")   # doctest: +SKIP
>>> job = pc.create_print_job(PrintTicket(job_name="hello"))               # doctest: +SKIP
>>> pc.send_document(job, b"%PDF-1.4 ...", format="application/pdf")       # doctest: +SKIP
>>> pc.get_job_status(job).state                                           # doctest: +SKIP
'Completed'
"""

from __future__ import annotations

import base64
import io
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import httpx

from .ticket import PRINT_NS, PrintTicket

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
WSA_NS = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
XOP_NS = "http://www.w3.org/2004/08/xop/include"
WSE_NS = "http://schemas.xmlsoap.org/ws/2004/08/eventing"
WSX_NS = "http://schemas.xmlsoap.org/ws/2004/09/mex"
WXF_NS = "http://schemas.xmlsoap.org/ws/2004/09/transfer"
DIALECT_ACTION = "http://schemas.xmlsoap.org/ws/2006/02/devprof/Action"
FAULT_ACTION = f"{WSA_NS}/fault"
ANONYMOUS = f"{WSA_NS}/role/anonymous"

PRINTER_ELEMENTS = (
    "PrinterDescription",
    "PrinterConfiguration",
    "PrinterStatus",
    "DefaultPrintTicket",
    "PrinterCapabilities",
)

# Faults the WS-Print notes define (docs/reference/faults.md), local names.
KNOWN_SUBCODES = frozenset(
    {
        "ActionNotSupported",
        "InvalidArgs",
        "OperationFailed",
        "ServerErrorTemporaryError",
        "ServerErrorInternalError",
        "ServerErrorNotAcceptingJobs",
        "ClientErrorJobIdNotFound",
        "ClientErrorMultipleDocumentsNotSupported",
        "ClientErrorLastDocumentAlreadySent",
        "ClientErrorFormatNotSupported",
        "ClientErrorCompressionNotSupported",
        "ClientErrorDocumentURLSchemeNotSupported",
        "ClientErrorDocumentURLNotFound",
        "ClientErrorConflictingRequiredParameters",
        "ServerErrorAddDocumentNotSupported",
        "ServerErrorDocumentActive",
        "ServerErrorJobCancelled",
    }
)


def q(local: str) -> str:
    """ElementTree tag for a print-namespace element."""
    return f"{{{PRINT_NS}}}{local}"


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---- errors -------------------------------------------------------------------------------


class PrintError(RuntimeError):
    """Base for everything this client raises."""


class HttpError(PrintError):
    """A non-2xx reply that was not a parseable SOAP fault."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body[:200]!r}")


class SoapFault(PrintError):
    """A SOAP 1.2 Fault. ``subcode`` is the QName text as sent (``wprt:ClientErrorJobIdNotFound``),
    ``subcode_local`` its local part, ``subcode_ns`` the namespace its prefix resolved to."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str | None,
        subcode: str | None,
        subcode_ns: str | None,
        reason: str,
        detail: str | None,
        body: str,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.subcode = subcode
        self.subcode_ns = subcode_ns
        self.subcode_local = subcode.rsplit(":", 1)[-1] if subcode else None
        self.reason = reason
        self.detail = detail
        self.body = body
        super().__init__(f"SOAP fault {code or '?'}/{self.subcode_local or '-'}: {reason}")

    @property
    def is_sender(self) -> bool:
        return (self.code or "").rsplit(":", 1)[-1] == "Sender"

    @property
    def is_receiver(self) -> bool:
        return (self.code or "").rsplit(":", 1)[-1] == "Receiver"


class JobIdNotFound(SoapFault):
    """``ClientErrorJobIdNotFound``."""


class FormatNotSupported(SoapFault):
    """``ClientErrorFormatNotSupported``."""


class NotAcceptingJobs(SoapFault):
    """``ServerErrorNotAcceptingJobs``."""


class ActionNotSupported(SoapFault):
    """``wsa:ActionNotSupported``."""


_SUBCLASSES: dict[str, type[SoapFault]] = {
    "ClientErrorJobIdNotFound": JobIdNotFound,
    "ClientErrorFormatNotSupported": FormatNotSupported,
    "ServerErrorNotAcceptingJobs": NotAcceptingJobs,
    "ActionNotSupported": ActionNotSupported,
}


def parse_fault(status_code: int, body: str) -> SoapFault | None:
    """Return the fault carried by ``body`` or None when it is not a SOAP fault."""
    try:
        root, ns = _parse_with_ns(body)
    except ET.ParseError:
        return None
    soap_body = next((c for c in root if c.tag == f"{{{SOAP_NS}}}Body"), None)
    fault = None if soap_body is None else next((c for c in soap_body if c.tag == f"{{{SOAP_NS}}}Fault"), None)
    if fault is None:
        return None

    def child(parent: ET.Element | None, name: str) -> ET.Element | None:
        if parent is None:
            return None
        return next((c for c in parent if c.tag == f"{{{SOAP_NS}}}{name}"), None)

    code_el = child(fault, "Code")
    value = child(code_el, "Value")
    code = value.text.strip() if value is not None and value.text else None
    # The deepest Subcode wins (Subcode may nest).
    sub_el = child(code_el, "Subcode")
    sub_text: str | None = None
    while sub_el is not None:
        v = child(sub_el, "Value")
        if v is not None and v.text:
            sub_text = v.text.strip()
        sub_el = child(sub_el, "Subcode")
    sub_ns = None
    if sub_text and ":" in sub_text:
        sub_ns = ns.get(sub_text.split(":", 1)[0])
    reason_el = child(child(fault, "Reason"), "Text")
    reason = (reason_el.text or "").strip() if reason_el is not None else ""
    detail_el = child(fault, "Detail")
    detail = ET.tostring(detail_el, encoding="unicode") if detail_el is not None else None
    cls = _SUBCLASSES.get(sub_text.rsplit(":", 1)[-1], SoapFault) if sub_text else SoapFault
    return cls(
        status_code=status_code,
        code=code,
        subcode=sub_text,
        subcode_ns=sub_ns,
        reason=reason,
        detail=detail,
        body=body,
    )


def _parse_with_ns(text: str) -> tuple[ET.Element, dict[str, str]]:
    ns: dict[str, str] = {}
    root: ET.Element | None = None
    for event, obj in ET.iterparse(io.StringIO(text), events=("start-ns", "start")):
        if event == "start-ns":
            prefix, uri = obj  # type: ignore[misc]
            ns.setdefault(prefix, uri)
        elif root is None:
            root = obj  # type: ignore[assignment]
    if root is None:
        raise ET.ParseError("empty document")
    return root, ns


# ---- results ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ElementData:
    """One ``ElementData`` of a GetPrinterElements / GetJobElements reply."""

    name: str
    valid: bool
    element: ET.Element | None  # the child named like the request, if any

    @property
    def local_name(self) -> str:
        return self.name.rsplit(":", 1)[-1]

    def text(self, path: str) -> str | None:
        """Text of the descendant at ``A/B/C`` (print-namespace local names)."""
        cur = self.element
        for part in path.split("/"):
            if cur is None:
                return None
            cur = next((c for c in cur.iter() if local(c.tag) == part and c is not cur), None)
        return cur.text.strip() if cur is not None and cur.text else None


@dataclass(frozen=True)
class PrinterElements:
    items: dict[str, ElementData]
    raw: str

    def __getitem__(self, name: str) -> ElementData:
        return self.items[name.rsplit(":", 1)[-1]]

    def __contains__(self, name: str) -> bool:
        return name.rsplit(":", 1)[-1] in self.items

    @property
    def printer_state(self) -> str | None:
        return self["PrinterStatus"].text("PrinterState") if "PrinterStatus" in self else None

    @property
    def queued_job_count(self) -> int | None:
        t = self["PrinterStatus"].text("QueuedJobCount") if "PrinterStatus" in self else None
        return int(t) if t is not None else None

    @property
    def document_formats(self) -> list[str]:
        """Allowed document formats from PrinterCapabilities (empty when absent)."""
        if "PrinterCapabilities" not in self or self["PrinterCapabilities"].element is None:
            return []
        el = self["PrinterCapabilities"].element
        assert el is not None
        out: list[str] = []
        for fmt in el.iter(q("Format")):
            out += [(a.text or "").strip() for a in fmt.iter(q("AllowedValue")) if a.text]
        return out


@dataclass(frozen=True)
class Subscription:
    """A WS-Eventing subscription as the printer granted it."""

    identifier: str
    expires: str


@dataclass(frozen=True)
class JobSummary:
    job_id: int
    state: str
    name: str
    user: str


@dataclass(frozen=True)
class JobStatus:
    job_id: int
    state: str
    reasons: list[str] = field(default_factory=list)
    k_octets_processed: int = 0
    media_sheets_completed: int = 0
    documents: int = 0


# ---- the client ---------------------------------------------------------------------------


class PrintClient:
    """Speaks WS-Print to one PrinterService endpoint.

    ``transport`` may be an ``httpx`` transport (tests use none; the embedded server is real HTTP
    on loopback). Operations return parsed values and raise :class:`SoapFault` / :class:`HttpError`.
    """

    def __init__(self, endpoint: str, *, timeout: float = 15.0, client: httpx.Client | None = None) -> None:
        self.endpoint = endpoint
        self._own = client is None
        self._http = client or httpx.Client(timeout=timeout)
        self.last_request: str | None = None
        self.last_response: str | None = None
        self.last_status: int | None = None

    def close(self) -> None:
        if self._own:
            self._http.close()

    def __enter__(self) -> PrintClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level ---------------------------------------------------------------------------
    @staticmethod
    def action_uri(action: str) -> str:
        """A short WS-Print operation name becomes its action URI; a full URI is kept."""
        return action if "://" in action else f"{PRINT_NS}/{action}"

    @staticmethod
    def envelope(
        action: str,
        body_xml: str,
        *,
        message_id: str | None = None,
        to: str | None = None,
        headers_xml: str = "",
    ) -> str:
        mid = message_id or f"urn:uuid:{uuid.uuid4()}"
        to_xml = f"<wsa:To>{esc(to)}</wsa:To>" if to else ""
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<soap:Envelope xmlns:soap="{SOAP_NS}" xmlns:wsa="{WSA_NS}" xmlns:wprt="{PRINT_NS}"'
            f' xmlns:wse="{WSE_NS}" xmlns:wsx="{WSX_NS}">'
            f"<soap:Header>{to_xml}<wsa:Action>{PrintClient.action_uri(action)}</wsa:Action>"
            f"<wsa:MessageID>{mid}</wsa:MessageID>"
            f"<wsa:ReplyTo><wsa:Address>{ANONYMOUS}</wsa:Address></wsa:ReplyTo>{headers_xml}</soap:Header>"
            f"<soap:Body>{body_xml}</soap:Body></soap:Envelope>"
        )

    def post(
        self,
        action: str,
        body_xml: str,
        *,
        content: bytes | None = None,
        content_type: str | None = None,
        headers_xml: str = "",
        allow_empty: bool = False,
    ) -> ET.Element:
        """POST a request and return the response ``Body`` child. Raises on fault/HTTP error.
        ``allow_empty`` accepts a reply with an empty Body (WS-Eventing ``Unsubscribe``).

        ``action`` is a short WS-Print operation (``SendDocument``) or a full action URI.
        """
        text = self.envelope(action, body_xml, to=self.endpoint, headers_xml=headers_xml)
        self.last_request = text
        payload = content if content is not None else text.encode("utf-8")
        r = self._http.post(
            self.endpoint,
            content=payload,
            headers={"Content-Type": content_type or f'application/soap+xml; charset=utf-8; action="{self.action_uri(action)}"'},
        )
        self.last_status = r.status_code
        self.last_response = r.text
        fault = parse_fault(r.status_code, r.text)
        if fault is not None:
            raise fault
        if r.status_code >= 300:
            raise HttpError(r.status_code, r.text)
        root = ET.fromstring(r.text)
        body = next((c for c in root if c.tag == f"{{{SOAP_NS}}}Body"), None)
        if allow_empty and body is not None and len(body) == 0:
            return ET.Element("empty")
        if body is None or len(body) == 0:
            raise PrintError(f"reply to {action} has no SOAP Body content: {r.text[:200]!r}")
        return body[0]

    def raw_post(self, content: bytes, content_type: str = "application/soap+xml") -> httpx.Response:
        """Send arbitrary bytes (for malformed-request tests). No parsing, no raising."""
        return self._http.post(self.endpoint, content=content, headers={"Content-Type": content_type})

    # -- operations --------------------------------------------------------------------------
    def get_printer_elements(self, names: Sequence[str] = PRINTER_ELEMENTS) -> PrinterElements:
        req = "".join(f"<wprt:Name>wprt:{esc(n)}</wprt:Name>" for n in names)
        body = f"<wprt:GetPrinterElementsRequest><wprt:RequestedElements>{req}</wprt:RequestedElements></wprt:GetPrinterElementsRequest>"
        resp = self.post("GetPrinterElements", body)
        return PrinterElements(_element_data(resp, "PrinterElements"), self.last_response or "")

    def create_print_job(self, ticket: PrintTicket | None = None) -> int:
        t = ticket or PrintTicket()
        resp = self.post("CreatePrintJob", f"<wprt:CreatePrintJobRequest>{t.to_xml()}</wprt:CreatePrintJobRequest>")
        job = next((c for c in resp.iter() if local(c.tag) == "JobId"), None)
        if job is None or not (job.text or "").strip().isdigit():
            raise PrintError(f"CreatePrintJobResponse without a JobId: {self.last_response!r}")
        return int(job.text.strip())  # type: ignore[union-attr]

    def send_document(
        self,
        job_id: int,
        data: bytes,
        *,
        format: str = "application/octet-stream",
        compression: str = "None",
        last: bool = True,
        document_id: int = 1,
        name: str | None = None,
        mtom: bool = False,
        boundary: str | None = None,
    ) -> None:
        """SendDocument with the data inline (base64) or as an MTOM/XOP attachment."""
        desc = (
            f"<wprt:DocumentDescription><wprt:DocumentId>{document_id}</wprt:DocumentId>"
            f"<wprt:Compression>{esc(compression)}</wprt:Compression><wprt:Format>{esc(format)}</wprt:Format>"
            + (f"<wprt:DocumentName>{esc(name)}</wprt:DocumentName>" if name else "")
            + "</wprt:DocumentDescription>"
        )
        head = f"<wprt:SendDocumentRequest><wprt:JobId>{job_id}</wprt:JobId>{desc}<wprt:LastDocument>{str(last).lower()}</wprt:LastDocument>"
        if not mtom:
            b64 = base64.b64encode(data).decode("ascii")
            self.post("SendDocument", f"{head}<wprt:DocumentData>{b64}</wprt:DocumentData></wprt:SendDocumentRequest>")
            return
        bnd = boundary or f"MIMEBoundary_{uuid.uuid4().hex}"
        root_id, doc_id = f"root.{uuid.uuid4().hex}@fakeprint", f"doc.{uuid.uuid4().hex}@fakeprint"
        body = f'{head}<wprt:DocumentData><xop:Include xmlns:xop="{XOP_NS}" href="cid:{doc_id}"/></wprt:DocumentData></wprt:SendDocumentRequest>'
        envelope = self.envelope("SendDocument", body, to=self.endpoint)
        self.last_request = envelope
        payload = b"".join(
            [
                f"--{bnd}\r\n".encode(),
                b'Content-Type: application/xop+xml; charset=utf-8; type="application/soap+xml"\r\n',
                b"Content-Transfer-Encoding: 8bit\r\n",
                f"Content-ID: <{root_id}>\r\n\r\n".encode(),
                envelope.encode("utf-8"),
                f"\r\n--{bnd}\r\n".encode(),
                b"Content-Type: application/octet-stream\r\n",
                b"Content-Transfer-Encoding: binary\r\n",
                f"Content-ID: <{doc_id}>\r\n\r\n".encode(),
                data,
                f"\r\n--{bnd}--\r\n".encode(),
            ]
        )
        ctype = f'multipart/related; type="application/xop+xml"; boundary="{bnd}"; start="<{root_id}>"; start-info="application/soap+xml"'
        self.post("SendDocument", body, content=payload, content_type=ctype)

    def get_job_elements(self, job_id: int, names: Sequence[str] = ("JobStatus", "PrintTicket")) -> dict[str, ElementData]:
        req = "".join(f"<wprt:Name>wprt:{esc(n)}</wprt:Name>" for n in names)
        body = f"<wprt:GetJobElementsRequest><wprt:JobId>{job_id}</wprt:JobId><wprt:RequestedElements>{req}</wprt:RequestedElements></wprt:GetJobElementsRequest>"
        return _element_data(self.post("GetJobElements", body), "JobElements")

    def get_job_status(self, job_id: int) -> JobStatus:
        """GetJobElements(JobStatus) as a :class:`JobStatus`. Raises :class:`JobIdNotFound`-style
        faults the server sends and LookupError when it answers ``Valid="false"`` instead."""
        data = self.get_job_elements(job_id, ("JobStatus",)).get("JobStatus")
        if data is None or not data.valid or data.element is None:
            raise LookupError(f"job {job_id}: JobStatus not valid")
        el = data.element
        t = lambda n: next((c.text or "" for c in el.iter() if local(c.tag) == n), "").strip()  # noqa: E731
        reasons = [(c.text or "").strip() for c in el.iter() if local(c.tag) == "JobStateReason"]
        return JobStatus(
            job_id=int(t("JobId") or job_id),
            state=t("JobState"),
            reasons=reasons,
            k_octets_processed=int(t("KOctetsProcessed") or 0),
            media_sheets_completed=int(t("MediaSheetsCompleted") or 0),
            documents=int(t("NumberOfDocuments") or 0),
        )

    def get_job_ticket(self, job_id: int) -> PrintTicket:
        data = self.get_job_elements(job_id, ("PrintTicket",)).get("PrintTicket")
        if data is None or not data.valid or data.element is None:
            raise LookupError(f"job {job_id}: PrintTicket not valid")
        return PrintTicket.from_element(data.element)

    def _summaries(self, action: str, wrapper: str) -> list[JobSummary]:
        resp = self.post(action, f"<wprt:{action}Request/>")
        out = []
        for s in resp.iter(q("JobSummary")):
            t = lambda n, s=s: next((c.text or "" for c in s if local(c.tag) == n), "").strip()  # noqa: E731
            out.append(JobSummary(int(t("JobId")), t("JobState"), t("JobName"), t("JobOriginatingUserName")))
        return out

    def get_active_jobs(self) -> list[JobSummary]:
        return self._summaries("GetActiveJobs", "ActiveJobs")

    def get_job_history(self) -> list[JobSummary]:
        return self._summaries("GetJobHistory", "JobHistory")

    def cancel_job(self, job_id: int) -> None:
        self.post("CancelJob", f"<wprt:CancelJobRequest><wprt:JobId>{job_id}</wprt:JobId></wprt:CancelJobRequest>")

    def set_event_rate(self, seconds: int) -> None:
        self.post("SetEventRate", f"<wprt:SetEventRateRequest><wprt:EventRate>{seconds}</wprt:EventRate></wprt:SetEventRateRequest>")

    def call(self, action: str, body_xml: str) -> ET.Element:
        """Any operation by short name with a hand-written body (for unusual-request tests)."""
        return self.post(action, body_xml)

    # -- WS-Transfer / WS-MetadataExchange (device or service endpoint) -----------------------
    def transfer_get(self) -> ET.Element:
        """WS-Transfer ``Get``: the ``wsx:Metadata`` document."""
        return self.post(f"{WXF_NS}/Get", "")

    def get_metadata(self, dialect: str | None = None) -> ET.Element:
        """WS-MetadataExchange ``GetMetadata`` (optionally one ``dialect``)."""
        d = f"<wsx:Dialect>{esc(dialect)}</wsx:Dialect>" if dialect else ""
        return self.post(f"{WSX_NS}/GetMetadata/Request", f"<wsx:GetMetadata>{d}</wsx:GetMetadata>")

    # -- WS-Eventing (PrinterService endpoint) ------------------------------------------------
    def subscribe(
        self,
        notify_to: str,
        *,
        actions: Sequence[str] | None = None,
        end_to: str | None = None,
        expires: str | None = None,
    ) -> Subscription:
        """Subscribe ``notify_to`` (a loopback URL) to events. ``actions`` are short event names
        (``JobEndStateEvent``) or URIs; ``None`` subscribes to everything. ``expires`` is an
        ``xs:duration`` such as ``PT5M``."""
        parts = []
        if end_to:
            parts.append(f"<wse:EndTo><wsa:Address>{esc(end_to)}</wsa:Address></wse:EndTo>")
        parts.append(f"<wse:Delivery><wse:NotifyTo><wsa:Address>{esc(notify_to)}</wsa:Address></wse:NotifyTo></wse:Delivery>")
        if expires:
            parts.append(f"<wse:Expires>{esc(expires)}</wse:Expires>")
        if actions:
            uris = " ".join(self.action_uri(a) for a in actions)
            parts.append(f'<wse:Filter Dialect="{DIALECT_ACTION}">{esc(uris)}</wse:Filter>')
        resp = self.post(f"{WSE_NS}/Subscribe", f"<wse:Subscribe>{''.join(parts)}</wse:Subscribe>")
        ident = next((c.text or "" for c in resp.iter() if local(c.tag) == "Identifier"), "").strip()
        exp = next((c.text or "" for c in resp.iter() if local(c.tag) == "Expires"), "").strip()
        if not ident:
            raise PrintError(f"SubscribeResponse without an Identifier: {self.last_response!r}")
        return Subscription(ident, exp)

    @staticmethod
    def _ident(sub: Subscription | str) -> str:
        ident = sub.identifier if isinstance(sub, Subscription) else sub
        return f"<wse:Identifier>{esc(ident)}</wse:Identifier>"

    def renew(self, sub: Subscription | str, expires: str = "PT5M") -> str:
        resp = self.post(f"{WSE_NS}/Renew", f"<wse:Renew><wse:Expires>{esc(expires)}</wse:Expires></wse:Renew>", headers_xml=self._ident(sub))
        return next((c.text or "" for c in resp.iter() if local(c.tag) == "Expires"), "").strip()

    def subscription_status(self, sub: Subscription | str) -> str:
        resp = self.post(f"{WSE_NS}/GetStatus", "<wse:GetStatus/>", headers_xml=self._ident(sub))
        return next((c.text or "" for c in resp.iter() if local(c.tag) == "Expires"), "").strip()

    def unsubscribe(self, sub: Subscription | str) -> None:
        self.post(f"{WSE_NS}/Unsubscribe", "<wse:Unsubscribe/>", headers_xml=self._ident(sub), allow_empty=True)

    def print_document(self, data: bytes, *, format: str = "application/octet-stream", ticket: PrintTicket | None = None, mtom: bool = False) -> int:
        """CreatePrintJob + one SendDocument(last=true); returns the job id."""
        job = self.create_print_job(ticket)
        self.send_document(job, data, format=format, mtom=mtom)
        return job


def _element_data(resp: ET.Element, wrapper: str) -> dict[str, ElementData]:
    holder = next((c for c in resp.iter() if local(c.tag) == wrapper), None)
    if holder is None:
        raise PrintError(f"reply has no {wrapper} element")
    out: dict[str, ElementData] = {}
    for ed in holder:
        if local(ed.tag) != "ElementData":
            continue
        name = ed.get("Name", "")
        key = name.rsplit(":", 1)[-1]
        kid = next(iter(ed), None)
        out[key] = ElementData(name, ed.get("Valid", "false").lower() in ("true", "1"), kid)
    return out


def element_names(items: Iterable[ElementData]) -> list[str]:
    return [i.local_name for i in items]


__all__ = [
    "PrintClient",
    "PrintTicket",
    "PrinterElements",
    "ElementData",
    "JobSummary",
    "JobStatus",
    "Subscription",
    "SoapFault",
    "HttpError",
    "PrintError",
    "JobIdNotFound",
    "FormatNotSupported",
    "NotAcceptingJobs",
    "ActionNotSupported",
    "KNOWN_SUBCODES",
    "parse_fault",
    "PRINTER_ELEMENTS",
    "PRINT_NS",
    "SOAP_NS",
]
