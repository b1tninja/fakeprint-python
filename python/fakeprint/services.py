"""Per-service configuration as DATA for :mod:`fakeprint.wsd_schema` / :mod:`fakeprint.wsd_docs`.

The parser core never mentions a service. Everything that differs between WSD services (where the
package lives, which namespace revisions are aliases, which concepts deserve their own page, which
operation names are worth reporting as present or absent) is listed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import _paths


@dataclass(frozen=True)
class ServiceProfile:
    key: str
    title: str
    #: repo that owns docs/reference/{schema,upstream}
    repo: str
    #: extra directories (relative to <repo>/docs/reference) searched for imports and loaded as schemas
    extra_dirs: tuple[str, ...] = ()
    #: alternate namespace URI -> canonical URI (message revisions that share local names)
    ns_alias: dict[str, str] = field(default_factory=dict)
    #: element / type local names that get a dedicated page under concepts/
    concepts: tuple[str, ...] = ()
    #: substrings marking ticket-like types for ticket.md
    ticket_markers: tuple[str, ...] = ("Ticket",)
    #: operation names to report as present or absent (administrative ops people expect)
    probe_operations: tuple[str, ...] = ()
    #: file stem order for the numbered per-file pages (others follow alphabetically)
    file_order: tuple[str, ...] = ()
    blurb: str = ""

    def reference_dir(self) -> Path:
        base = _paths.fakeprint_repo() if self.repo == "fakeprint" else _paths.fakescan_repo()
        return base / "docs" / "reference"

    def schema_dir(self) -> Path:
        return self.reference_dir() / "schema"

    def search_dirs(self) -> list[Path]:
        ref = self.reference_dir()
        return [ref / "schema", *[ref / d for d in self.extra_dirs]]


PRINT = ServiceProfile(
    key="print",
    title="WS-Print (WSD Print Service)",
    repo="fakeprint",
    extra_dirs=("upstream/print-v2",),
    ns_alias={"http://schemas.microsoft.com/windows/2006/01/wdp/print": "http://schemas.microsoft.com/windows/2006/08/wdp/print"},
    concepts=(
        "JobState", "JobStateReasons", "JobStateReason", "JobStatus", "JobSummary", "JobDescription",
        "JobProcessing", "DocumentDescription", "DocumentProcessing", "PrinterState",
        "PrinterStateReasons", "PrinterStatus", "PrinterDescription", "PrinterConfiguration",
        "PrinterCapabilities", "DefaultPrintTicket", "PrintTicket", "ServiceTypes", "MediaSizeName",
        "MediaType", "MediaColor", "Copies", "Orientation", "PrintQuality", "Resolution", "Sides",
        "NumberUp", "PagesPerSheet", "Format", "Compression", "ElementData", "DeviceCondition",
    ),
    ticket_markers=("Ticket", "Capabilities", "JobValues", "DocumentValues"),
    probe_operations=(
        "ValidatePrintTicket", "PausePrinter", "ResumePrinter", "PurgeJobs", "PurgeJob", "PauseJob",
        "ResumeJob", "HoldJob", "ReleaseJob", "GetPrinterAttributes",
    ),
    file_order=("WellKnownValues", "Common", "Document", "Job", "Printer", "WDPPrint"),
    blurb="WS-Print service contract (namespace family .../windows/2006/08/wdp/print) plus the v1.1, v1.2 and v2.0 extension schemas.",
)

SCAN = ServiceProfile(
    key="scan",
    title="WS-Scan (WSD Scan Service)",
    repo="fakescan",
    ns_alias={"http://schemas.microsoft.com/windows/2006/01/wdp/scan": "http://schemas.microsoft.com/windows/2006/08/wdp/scan"},
    concepts=("JobState", "JobStateReason", "ScanTicket", "DocumentParameters", "Format", "ColorProcessing",
              "Resolution", "InputSource", "ScannerState", "ScannerStateReason"),
    ticket_markers=("Ticket",),
    probe_operations=("ValidatePrintTicket", "PauseScanner", "ResumeScanner"),
    file_order=("WellKnownValues", "Common", "Document", "Job", "Scanner", "WDPScan"),
    blurb="WS-Scan service contract (namespace family .../windows/2006/08/wdp/scan).",
)

PROFILES = {p.key: p for p in (PRINT, SCAN)}
