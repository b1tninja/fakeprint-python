"""HTTP client for the fakeprint control plane (``/health``, ``/v1/*``).

The daemon (``fakeprint.exe``, default port 3921) and the embedded :class:`fakeprint.Server`
serve the same routes (the library's ``fakeprint::server::router``): ``/health``, ``/v1/status``,
``/v1/jobs`` (``jobs`` = captured documents, ``print_jobs`` = the job table with history),
``/v1/jobs/{id}``, ``/v1/reset``, ``/v1/profile``, ``/v1/ctl``, ``/v1/faults`` (printer
conditions and injection rules) and ``/v1/timing``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:3921"

# Server-wide routes of the device manager: never prefixed with /d/<id>.
_SERVER_WIDE = re.compile(r"^/v1/(devices|profiles|registry|queue|reconcile)(/|$)")


class ApiError(httpx.HTTPStatusError):
    """A control-plane call answered with an HTTP error; ``str(e)`` carries the server's text."""


@dataclass(frozen=True)
class Capture:
    """One captured document (a row of ``/v1/jobs``)."""

    job_id: int
    path: str
    meta_path: str
    bytes: int
    document_format: str
    peer: str
    document_id: int = 1

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Capture:
        return cls(
            document_id=int(d.get("document_id", 1)),
            job_id=int(d["job_id"]),
            path=str(d["path"]),
            meta_path=str(d.get("meta_path", "")),
            bytes=int(d["bytes"]),
            document_format=str(d.get("document_format", "")),
            peer=str(d.get("peer", "")),
        )


@dataclass(frozen=True)
class JobDocument:
    """One document of a job (``print_jobs[].documents[]``)."""

    id: int
    format: str
    compression: str
    bytes: int
    path: str | None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> JobDocument:
        return cls(int(d["id"]), str(d.get("format", "")), str(d.get("compression", "")), int(d.get("bytes", 0)), d.get("path"))


TERMINAL_STATES = frozenset({"Completed", "Canceled", "Aborted"})


@dataclass(frozen=True)
class Job:
    """A print job the printer remembers (a ``print_jobs`` row): active or in history."""

    job_id: int
    name: str
    user: str
    state: str
    reasons: list[str]
    documents: list[JobDocument]
    k_octets_processed: int
    media_sheets_completed: int
    last_document_received: bool
    #: The IPP submission (``print_jobs[].ipp``: ``attributes``, ``scaling``, ``media``, ``effective_scale``...); None for a WS-Print job.
    ipp: dict[str, Any] | None = None
    #: Optional test-page analysis summary (``Client.job(id, analysis=True)`` / ``Client.job_analysis``); never filled by the daemon.
    analysis: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Job:
        return cls(
            job_id=int(d["id"]),
            name=str(d.get("name", "")),
            user=str(d.get("user", "")),
            state=str(d["state"]),
            reasons=[str(r) for r in d.get("reasons", [])],
            documents=[JobDocument.from_json(x) for x in d.get("documents", [])],
            k_octets_processed=int(d.get("k_octets_processed", 0)),
            media_sheets_completed=int(d.get("media_sheets_completed", 0)),
            last_document_received=bool(d.get("last_document_received", False)),
            ipp=d.get("ipp"),
        )

    @property
    def print_scaling(self) -> str | None:
        """The IPP ``print-scaling`` keyword in effect (the job's, else the printer's default); None when the printer does not advertise it
        or the job did not come over IPP."""
        return (self.ipp or {}).get("scaling")

    @property
    def effective_scale(self) -> float | None:
        """The uniform scale the printer would apply to the first page of the stored PDF, from its page size, the job's media and
        ``print-scaling`` (the stored document itself is not rescaled); None when unknown (not a flat PDF, not an IPP job)."""
        v = (self.ipp or {}).get("effective_scale")
        return None if v is None else float(v)

    @property
    def active(self) -> bool:
        return self.state not in TERMINAL_STATES

    @property
    def bytes_received(self) -> int:
        return sum(x.bytes for x in self.documents)

    @property
    def document_format(self) -> str:
        return self.documents[0].format if self.documents else ""


@dataclass(frozen=True)
class Status:
    """``GET /v1/status``; ``raw`` keeps every field."""

    raw: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.raw.get("name", ""))

    @property
    def profile_id(self) -> str:
        return str(self.raw.get("profile_id", ""))

    @property
    def device_uuid(self) -> str:
        return str(self.raw.get("device_uuid", ""))

    @property
    def device_id(self) -> str:
        """The multi-device id this status belongs to (``""`` from a library without devices)."""
        return str(self.raw.get("device_id", ""))

    @property
    def device_endpoint(self) -> str:
        return str(self.raw.get("device_endpoint", ""))

    @property
    def service_endpoint(self) -> str:
        return str(self.raw.get("service_endpoint", ""))

    @property
    def active_jobs(self) -> int:
        return int(self.raw.get("active_jobs", 0))

    @property
    def captured_jobs(self) -> int:
        return int(self.raw.get("captured_jobs", 0))

    @property
    def document_formats(self) -> list[str]:
        return [str(x) for x in self.raw.get("document_formats", [])]

    @property
    def available_profiles(self) -> list[str]:
        return [str(x) for x in self.raw.get("available_profiles", [])]

    @property
    def crate_version(self) -> str:
        return str(self.raw.get("crate_version", ""))

    @property
    def printer_state(self) -> str:
        return str(self.raw.get("printer_state", ""))

    @property
    def subscriptions(self) -> int:
        return int(self.raw.get("subscriptions", 0))

    @property
    def validation(self) -> dict[str, Any]:
        """``{"enabled", "checked", "problems", "recent"}`` of the reply validator."""
        return dict(self.raw.get("validation") or {})

    @property
    def wsd_actions(self) -> dict[str, int]:
        """SOAP short-action hit counts (``{"CreatePrintJob": 1, ...}``)."""
        return {str(k): int(v) for k, v in (self.raw.get("wsd_actions") or {}).items()}

    @property
    def ipp_actions(self) -> dict[str, int]:
        return {str(k): int(v) for k, v in (self.raw.get("ipp_actions") or {}).items()}


class Client:
    """Scripts a running fakeprint's ``/v1/*`` routes. Loopback by default; pass ``base_url``."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 10.0,
        http: httpx.Client | None = None,
        device: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.device = device or None
        self._own = http is None
        self._http = http or httpx.Client(base_url=self.base_url, timeout=timeout)

    @property
    def prefix(self) -> str:
        """``/d/<id>`` for a client scoped to one device of a multi-device server, else ``""``."""
        return f"/d/{self.device}" if self.device else ""

    def _scoped(self, path: str) -> str:
        # Only the control plane (/v1/*) is per device; /health and the manager routes
        # (/v1/devices, /v1/queue, ...) are server-wide.
        if self.device and path.startswith("/v1/") and not _SERVER_WIDE.match(path):
            return self.prefix + path
        return path

    def close(self) -> None:
        if self._own:
            self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        path = self._scoped(path)
        r = self._http.request(method, path, **kw)
        if r.status_code >= 400:
            raise ApiError(f"{method} {path}: {r.status_code} {r.text.strip()[:300]}", request=r.request, response=r)
        return r

    def health(self) -> bool:
        return self._req("GET", "/health").text.strip() == "ok"

    def status(self) -> Status:
        return Status(self._req("GET", "/v1/status").json())

    def jobs(self) -> list[Capture]:
        """Captured documents, oldest first."""
        return [Capture.from_json(j) for j in self._req("GET", "/v1/jobs").json().get("jobs", [])]

    def print_jobs(self) -> list[Job]:
        """The job table, active and history, ascending by id."""
        return [Job.from_json(j) for j in self._req("GET", "/v1/jobs").json().get("print_jobs", [])]

    def job(self, job_id: int, *, analysis: bool = False) -> Job | None:
        """``GET /v1/jobs/{id}``; None when the printer does not know the job.  ``analysis=True`` also fills ``Job.analysis`` with the
        :meth:`job_analysis` summary of the received document (lazy, Python-side; see there)."""
        j = self._job(job_id)
        if j is not None and analysis:
            from dataclasses import replace

            j = replace(j, analysis=self.job_analysis(j)["summary"])
        return j

    def _job(self, job_id: int) -> Job | None:
        r = self._http.get(self._scoped(f"/v1/jobs/{job_id}"))
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise ApiError(f"GET /v1/jobs/{job_id}: {r.status_code} {r.text.strip()[:300]}", request=r.request, response=r)
        return Job.from_json(r.json())

    def job_analysis(self, job: int | Job, *, params: Any = None, dpi: float = 150.0, pages: str | int = "first",
                     detail: str = "fast") -> dict[str, Any]:
        """Analyse the document a job RECEIVED against the fakeprint print test page (``fakeprint.testpage``).

        Computed lazily, in Python, on request: the daemon (Rust) has no rasteriser, so there is no ``/v1/jobs/<id>/analysis`` route and
        the daemon UI shows only the job rows.  The stored document is read from the path in the job row, so the daemon must share a
        filesystem with this client (the embedded ``Server`` and a local daemon do).  Returns ``{"job_id", "document", "summary",
        "analysis"}``: ``summary`` is :func:`fakeprint.testpage_analyze.brief` (scale, rotation / flip verdict, clipped edges, page /
        copy / side, QR text, or ``is_test_page: false`` with the reason); ``analysis`` is the full report.  ``params``: the
        :class:`fakeprint.testpage.PageParams` the page was generated from (default: recovered from the PDF).  Needs Pillow, pypdfium2 and
        fakescan (``pip install fakeprint[testpage]``)."""
        from .testpage_analyze import analyze_document, brief

        jid = job.job_id if isinstance(job, Job) else int(job)
        row = job if isinstance(job, Job) else self.job(jid)
        path = fmt = None
        if row is not None and row.documents:
            path, fmt = row.documents[0].path, row.documents[0].format
        if not path:
            cap = next((c for c in self.jobs() if c.job_id == jid), None)
            if cap is not None:
                path, fmt = cap.path, cap.document_format
        if not path or not Path(path).is_file():
            raise FileNotFoundError(f"job {jid}: the stored document {path!r} is not readable from this machine")
        data = Path(path).read_bytes()
        rep = analyze_document(data, (fmt or "application/pdf").split(";")[0], params, dpi=dpi, pages=pages, detail=detail)
        return {"job_id": jid, "document": {"path": path, "format": fmt, "bytes": len(data)}, "summary": brief(rep), "analysis": rep}

    def reset(self) -> Status:
        return Status(self._req("POST", "/v1/reset").json())

    def set_profile(self, profile_id: str) -> Status:
        return Status(self._req("POST", "/v1/profile", json={"id": profile_id}).json())

    def set_profile_doc(self, profile: dict[str, Any]) -> Status:
        """Install a schema-2 profile document (``{"profile": {...}}``)."""
        return Status(self._req("POST", "/v1/profile", json={"profile": profile}).json())

    def faults(self) -> dict[str, Any]:
        """``GET /v1/faults``: ``{"pending": {...}, "active": {...}}``."""
        return self._req("GET", "/v1/faults").json()

    def set_faults(self, body: dict[str, Any] | None = None, **flags: Any) -> dict[str, Any]:
        """``POST /v1/faults``: ``paper_out=True``, ``raise_=[{"name": ...}]`` (also ``raise``),
        ``clear=["CoverOpen"]``, ``reset=True``, ``fail_next_document="Format"``, ..."""
        payload = dict(body or {})
        for k, v in flags.items():
            payload["raise" if k == "raise_" else k] = v
        return self._req("POST", "/v1/faults", json=payload).json()

    def clear_faults(self) -> dict[str, Any]:
        """``DELETE /v1/faults``: clear every condition and flag."""
        return self._req("DELETE", "/v1/faults").json()

    def inject(self, rule: dict[str, Any] | None = None, *, clear: bool = False, **fields: Any) -> dict[str, Any]:
        """Arm one server-side injection rule (``POST /v1/faults {"inject": [rule]}``, see fakeprint
        ``docs/injection.md``); ``clear=True`` drops the pending rules first. Raises
        :class:`ApiError` (400) for a malformed rule."""
        body: dict[str, Any] = {"inject": [{**(rule or {}), **fields}]}
        if clear:
            body["inject_clear"] = True
        return self._req("POST", "/v1/faults", json=body).json()

    def clear_inject(self) -> dict[str, Any]:
        """Drop the pending injection rules only (``{"inject_clear": true}``)."""
        return self._req("POST", "/v1/faults", json={"inject_clear": True}).json()

    def pending_inject(self) -> list[dict[str, Any]]:
        return list(self.faults()["inject"]["pending"])

    def timing(self, **patch: Any) -> dict[str, Any]:
        """``GET /v1/timing``; with ``preset=`` (``instant``/``realistic``/``default``) or any of
        ``processing_base_ms``, ``processing_per_mib_ms``, ``document_timeout_ms``,
        ``terminating_ms``, ``document_busy_ms``, ``bytes_per_sheet``: ``POST /v1/timing``."""
        if not patch:
            return self._req("GET", "/v1/timing").json()
        return self._req("POST", "/v1/timing", json=patch).json()

    def levels(
        self, consumables: dict[str, int] | None = None, inputs: dict[str, int] | None = None
    ) -> dict[str, Any]:
        """``POST /d/{id}/v1/levels`` ``{consumables: {"Black": 15}, inputs: {"Tray1": 0}}``: runtime
        levels, never a capability change. Returns the ``live`` block."""
        body: dict[str, Any] = {}
        if consumables:
            body["consumables"] = dict(consumables)
        if inputs:
            body["inputs"] = dict(inputs)
        return self._req("POST", "/v1/levels", json=body).json()

    def devices(self) -> list[dict[str, Any]]:
        """``GET /v1/devices``: one ``DeviceInfo`` object per printer (docs/multi-device.md 2.2)."""
        return list(self._req("GET", "/v1/devices").json())

    def ctl(self, cmd: str, **args: Any) -> dict[str, Any]:
        """``POST /v1/ctl`` (status, health, jobs, reset, profile, faults, ...). A client scoped to a
        device sends ``args.device`` so the daemon runs the command on that printer."""
        if self.device and "device" not in args:
            args["device"] = self.device
        return self._req("POST", "/v1/ctl", json={"cmd": cmd, "args": args}).json()

    def ctl_info(self) -> dict[str, Any]:
        return self._req("GET", "/v1/ctl").json()
