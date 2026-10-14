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
from .manager import BadRequest, Conflict, Forbidden, Manager, ManagerError, NotYet, PipeTransport, UnknownDevice
from .ticket import PrintTicket

if TYPE_CHECKING:
    from .server import Device, Server, start

__version__ = "0.1.0"

#: Every fakeprint/fakescan device UUID starts with this prefix; it (not the friendly name) is how
#: a device is recognised as ours. Keep in sync with the Rust crates (``FAKE_UUID_PREFIX``).
FAKE_UUID_PREFIX = "00000000-0000-4000-8000-"


def is_fake_uuid(value: object) -> bool:
    """True if ``value`` is a UUID in the fake namespace (:data:`FAKE_UUID_PREFIX`).

    Case-insensitive; accepts surrounding braces, ``urn:uuid:`` and ``uuid:`` forms. Anything that is
    not a string (or not a full 36-character UUID) is ``False``."""
    if not isinstance(value, str):
        return False
    v = value.strip().lower()
    for pre in ("urn:uuid:", "uuid:"):
        if v.startswith(pre):
            v = v[len(pre) :]
            break
    v = v.strip("{}").strip()
    return len(v) == 36 and v.startswith(FAKE_UUID_PREFIX)

__all__ = [
    "FAKE_UUID_PREFIX",
    "is_fake_uuid",
    "ActionNotSupported",
    "ApiError",
    "Capture",
    "BadRequest",
    "Client",
    "Conflict",
    "Device",
    "Forbidden",
    "FormatNotSupported",
    "HttpError",
    "Job",
    "JobDocument",
    "JobIdNotFound",
    "JobStatus",
    "Manager",
    "ManagerError",
    "NotYet",
    "PipeTransport",
    "UnknownDevice",
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
    "registry",
    "manager",
    "ui",
    "tray",
    "lab",
    "ctl",
    "mcp_server",
]


def __getattr__(name: str) -> Any:
    # Server needs the compiled extension; the pure-Python parts import without it.
    if name in ("Server", "start", "Device"):
        from . import server

        return getattr(server, name)
    if name in ("registry", "ui", "tray", "lab", "ctl", "mcp_server"):
        import importlib

        return importlib.import_module(f"fakeprint.{name}")
    if name == "winprint":
        # Optional, Windows-only (ctypes over winspool.drv); raises ImportError elsewhere.
        import importlib

        return importlib.import_module("fakeprint.winprint")
    raise AttributeError(f"module 'fakeprint' has no attribute {name!r}")
