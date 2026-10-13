"""A typed builder for the WS-Print ``PrintTicket`` (job description, job and document processing).

Element order follows the schema's sequence (JobDescription, JobProcessing, DocumentProcessing),
so the output validates against ``wprt:PrintTicketType``. Any field named in ``must_honor`` is
written with ``wprt:MustHonor="true"``: the printer must then apply that exact value or fail the
request instead of substituting its own (see docs/reference/must-honor.md in the emulator repo).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

PRINT_NS = "http://schemas.microsoft.com/windows/2006/08/wdp/print"

# Ticket fields that can carry MustHonor, in the order the XML writes them.
HONORABLE = (
    "copies",
    "collate",
    "priority",
    "media_size",
    "media_type",
    "media_color",
    "pages_per_sheet",
    "nup_direction",
    "orientation",
    "resolution",
    "print_quality",
    "sides",
)


def _txt(v: object) -> str:
    return escape(str(v))


@dataclass
class PrintTicket:
    """One print ticket. ``None`` leaves a setting out so the printer default applies."""

    job_name: str = "fakeprint job"
    user: str = "fakeprint"
    copies: int | None = None
    collate: bool | None = None
    priority: int | None = None
    media_size: str | None = None  # e.g. "iso_a4_210x297mm", "na_letter_8.5x11in" (PWG self-describing names)
    media_type: str | None = None  # e.g. "stationery", "envelope"
    media_color: str | None = None  # e.g. "white"
    pages_per_sheet: int | None = None
    nup_direction: str | None = None  # RightDown / DownRight / LeftDown / DownLeft
    orientation: str | None = None  # Portrait / Landscape / ...
    resolution: tuple[int, int] | int | None = None  # (width, height) dpi, or one number for both
    print_quality: str | None = None
    sides: str | None = None  # OneSided / TwoSidedLongEdge / TwoSidedShortEdge
    must_honor: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        self.must_honor = frozenset(self.must_honor)

    # -- validation --------------------------------------------------------------------------
    def problems(self) -> list[str]:
        """Local range checks taken from the schema. Empty means nothing obviously wrong."""
        out: list[str] = []
        if len(self.job_name) > 255:
            out.append("job_name longer than 255 characters")
        if len(self.user) > 255:
            out.append("user longer than 255 characters")
        if self.copies is not None and self.copies < 1:
            out.append("copies must be >= 1")
        if self.priority is not None and not 1 <= self.priority <= 100:
            out.append("priority must be 1..100")
        if self.pages_per_sheet is not None and self.pages_per_sheet < 1:
            out.append("pages_per_sheet must be >= 1")
        unknown = self.must_honor - set(HONORABLE)
        if unknown:
            out.append(f"must_honor names unknown fields: {sorted(unknown)}")
        return out

    # -- XML ---------------------------------------------------------------------------------
    def _mh(self, name: str) -> str:
        return ' wprt:MustHonor="true"' if name in self.must_honor else ""

    def _leaf(self, tag: str, attr: str, value: object) -> str:
        return f"<wprt:{tag}{self._mh(attr)}>{_txt(value).lower() if isinstance(value, bool) else _txt(value)}</wprt:{tag}>"

    def to_xml(self) -> str:
        """The ``<wprt:PrintTicket>`` element. The caller binds the ``wprt`` prefix."""
        if bad := self.problems():
            raise ValueError("; ".join(bad))
        parts = [
            "<wprt:PrintTicket>",
            "<wprt:JobDescription>",
            f"<wprt:JobName>{_txt(self.job_name)}</wprt:JobName>",
            f"<wprt:JobOriginatingUserName>{_txt(self.user)}</wprt:JobOriginatingUserName>",
            "</wprt:JobDescription>",
        ]
        job: list[str] = []
        if self.copies is not None:
            job.append(self._leaf("Copies", "copies", self.copies))
        if self.collate is not None:
            job.append(f"<wprt:JobFinishings>{self._leaf('Collate', 'collate', self.collate)}</wprt:JobFinishings>")
        if self.priority is not None:
            job.append(self._leaf("Priority", "priority", self.priority))
        if job:
            parts += ["<wprt:JobProcessing>", *job, "</wprt:JobProcessing>"]
        doc: list[str] = []
        for tag, attr in (("MediaSizeName", "media_size"), ("MediaType", "media_type"), ("MediaColor", "media_color")):
            if (v := getattr(self, attr)) is not None:
                doc.append(self._leaf(tag, attr, v))
        if self.pages_per_sheet is not None:
            direction = self.nup_direction or "RightDown"
            doc.append(
                "<wprt:NumberUp>"
                + self._leaf("PagesPerSheet", "pages_per_sheet", self.pages_per_sheet)
                + self._leaf("Direction", "nup_direction", direction)
                + "</wprt:NumberUp>"
            )
        if self.orientation is not None:
            doc.append(self._leaf("Orientation", "orientation", self.orientation))
        if self.resolution is not None:
            w, h = (self.resolution, self.resolution) if isinstance(self.resolution, int) else self.resolution
            doc.append(f"<wprt:Resolution{self._mh('resolution')}><wprt:Width>{w}</wprt:Width><wprt:Height>{h}</wprt:Height></wprt:Resolution>")
        if self.print_quality is not None:
            doc.append(self._leaf("PrintQuality", "print_quality", self.print_quality))
        if self.sides is not None:
            doc.append(self._leaf("Sides", "sides", self.sides))
        if doc:
            parts += ["<wprt:DocumentProcessing>", *doc, "</wprt:DocumentProcessing>"]
        parts.append("</wprt:PrintTicket>")
        return "".join(parts)

    @classmethod
    def from_element(cls, el: ET.Element) -> PrintTicket:
        """Read a ``PrintTicket`` element (for example from GetJobElements)."""

        def find(*path: str) -> ET.Element | None:
            cur: ET.Element | None = el
            for p in path:
                if cur is None:
                    return None
                cur = next((c for c in cur if _local(c.tag) == p), None)
            return cur

        def text(*path: str) -> str | None:
            n = find(*path)
            return n.text.strip() if n is not None and n.text else None

        mh = f"{{{PRINT_NS}}}MustHonor"
        honored: set[str] = set()
        for tag, attr, path in (
            ("Copies", "copies", ("JobProcessing", "Copies")),
            ("Priority", "priority", ("JobProcessing", "Priority")),
            ("MediaSizeName", "media_size", ("DocumentProcessing", "MediaSizeName")),
            ("Orientation", "orientation", ("DocumentProcessing", "Orientation")),
            ("Sides", "sides", ("DocumentProcessing", "Sides")),
            ("PrintQuality", "print_quality", ("DocumentProcessing", "PrintQuality")),
        ):
            n = find(*path)
            if n is not None and n.get(mh, "").lower() in ("true", "1"):
                honored.add(attr)
        res = None
        if (w := text("DocumentProcessing", "Resolution", "Width")) is not None:
            h = text("DocumentProcessing", "Resolution", "Height") or w
            res = (int(w), int(h))
        collate = text("JobProcessing", "JobFinishings", "Collate")
        return cls(
            job_name=text("JobDescription", "JobName") or "",
            user=text("JobDescription", "JobOriginatingUserName") or "",
            copies=int(c) if (c := text("JobProcessing", "Copies")) else None,
            collate=None if collate is None else collate.lower() in ("true", "1"),
            priority=int(p) if (p := text("JobProcessing", "Priority")) else None,
            media_size=text("DocumentProcessing", "MediaSizeName"),
            media_type=text("DocumentProcessing", "MediaType"),
            media_color=text("DocumentProcessing", "MediaColor"),
            pages_per_sheet=int(n) if (n := text("DocumentProcessing", "NumberUp", "PagesPerSheet")) else None,
            nup_direction=text("DocumentProcessing", "NumberUp", "Direction"),
            orientation=text("DocumentProcessing", "Orientation"),
            resolution=res,
            print_quality=text("DocumentProcessing", "PrintQuality"),
            sides=text("DocumentProcessing", "Sides"),
            must_honor=frozenset(honored),
        )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


__all__ = ["PrintTicket", "PRINT_NS", "HONORABLE"]
