"""fakeprint: Python side of the fakeprint WSD printer emulator.

Native in-process server (``Server``), control-plane ``Client``, WS-Print SOAP ``PrintClient``,
``PrintTicket`` builder, plus the schema model and reference-doc generator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._paths import list_profiles as profiles
from .client import ApiError, Capture, Client, Job, JobDocument, Status
from .soap import (
    ActionNotSupported,
    FormatNotSupported,
    HttpError,
    JobIdNotFound,
    JobStatus,
    JobSummary,
    NotAcceptingJobs,
    PrintClient,
    PrintError,
    SoapFault,
    Subscription,
    parse_fault,
)
from .ticket import PrintTicket

if TYPE_CHECKING:
    from .server import Server, start

__version__ = "0.1.0"

__all__ = [
    "ActionNotSupported",
    "ApiError",
    "Capture",
    "Client",
    "FormatNotSupported",
    "HttpError",
    "Job",
    "JobDocument",
    "JobIdNotFound",
    "JobStatus",
    "JobSummary",
    "NotAcceptingJobs",
    "PrintClient",
    "PrintError",
    "PrintTicket",
    "Server",
    "SoapFault",
    "Status",
    "Subscription",
    "profiles",
    "start",
    "parse_fault",
    "wsd_schema",
    "wsd_docs",
    "services",
]


def __getattr__(name: str) -> Any:
    # Server needs the compiled extension; the pure-Python parts import without it.
    if name in ("Server", "start"):
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module 'fakeprint' has no attribute {name!r}")
