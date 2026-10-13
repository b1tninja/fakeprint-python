"""Typed data behind the device-manager UI and tray (same semantics as the Rust ``tray_model``).

The GUI itself is the daemon's ``GET /ui`` page and its in-daemon tray; this module gives Python the
same *data*: the profile list, one status row per printer, the plain-text table, the
green/amber/red summary level the tray icon uses and the "Add in Windows" guidance text.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .registry import NOT_LISTED, PROFILE_CHANGED, ManagedDevice

# Boolean members of ``DeviceInfo.live.faults`` that count as a raised fault (tray toggles).
FAULT_KEYS = ("paper_out", "toner_low", "cover_open", "paper_jam", "offline")
# A ``profile changed`` stale is held this long before it counts as an Add request (the tray model's grace).
PROFILE_STALE_GRACE_S = 20.0

_GUIDANCE = (
    'Add now: {name} ({n} of {total}). Open Settings > Bluetooth & devices > Printers & scanners > '
    'Add device, wait for "{name}" to appear and choose Add. Windows takes about 30 seconds; fakeprint '
    "announces the next printer only after this one is installed."
)


def add_guidance(friendly_name: str, position: int, total: int) -> str:
    """The text a human follows to Add one printer in Windows (identical to the tray's and the web UI's)."""
    return _GUIDANCE.format(name=friendly_name, n=position, total=total)


@dataclass
class ProfileInfo:
    """One element of ``GET /v1/profiles``."""

    id: str
    label: str = ""
    formats: list[str] = field(default_factory=list)
    color_modes: list[str] = field(default_factory=list)
    sides: list[str] = field(default_factory=list)
    media_sizes: list[str] = field(default_factory=list)
    input_bins: list[str] = field(default_factory=list)
    versions: dict[str, bool] = field(default_factory=dict)
    description: str = ""
    source: str = "daemon"

    @classmethod
    def parse(cls, raw: Mapping[str, Any], source: str = "daemon") -> ProfileInfo:
        def lst(k: str) -> list[str]:
            return [str(x) for x in (raw.get(k) or [])]

        return cls(
            id=str(raw.get("id") or ""),
            label=str(raw.get("label") or ""),
            formats=lst("formats"),
            color_modes=lst("color_modes"),
            sides=lst("sides"),
            media_sizes=lst("media_sizes"),
            input_bins=lst("input_bins"),
            versions={str(k): bool(v) for k, v in (raw.get("versions") or {}).items()},
            description=str(raw.get("description") or raw.get("label") or ""),
            source=source,
        )


def profiles_from_files(directory: Path) -> list[ProfileInfo]:
    """Best-effort ``ProfileInfo`` list from ``*.json`` in ``directory`` (unreadable files skipped)."""
    import json

    out: list[ProfileInfo] = []
    for p in sorted(Path(directory).glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            raw.setdefault("id", p.stem)
            out.append(ProfileInfo.parse(raw, source="files"))
    return out


@dataclass
class StatusRow:
    """One line of the printer table (what the web UI and the tray summarise)."""

    id: str
    friendly_name: str = ""
    uuid: str = ""
    profile: str = ""
    install_state: str = "new"
    needs_readd: bool = False
    reason: str | None = None
    stale_at: str | None = None
    announcing: bool = False
    queue_position: int | None = None
    printer_state: str = ""
    active_jobs: int = 0
    captured_jobs: int = 0
    faults: list[str] = field(default_factory=list)
    klass: str = ""
    service_endpoint: str | None = None
    device_endpoint: str | None = None
    ipp_endpoint: str | None = None
    last_peer: str | None = None

    @classmethod
    def from_device(cls, d: ManagedDevice) -> StatusRow:
        inst = d.install
        live = d.live or {}
        return cls(
            id=d.id,
            friendly_name=d.friendly_name,
            uuid=d.uuid,
            profile=d.profile,
            install_state=d.state,
            needs_readd=d.needs_readd,
            reason=d.needs_readd_reason or (inst.reason if inst and (d.needs_readd or inst.stale) else None),
            stale_at=inst.stale_at if inst else None,
            announcing=d.announcing,
            queue_position=d.queue_position,
            printer_state=str(live.get("printer_state") or ""),
            active_jobs=int(live.get("active_jobs") or 0),
            captured_jobs=int(live.get("captured_jobs") or 0),
            faults=faults_from_live(live),
            klass=d.klass,
            service_endpoint=d.service_endpoint,
            device_endpoint=d.device_endpoint,
            ipp_endpoint=d.ipp_endpoint,
            last_peer=inst.last_peer if inst else None,
        )

    @property
    def offline(self) -> bool:
        """Stale only because Windows does not list it any more: offline, not an Add request."""
        return self.install_state == "stale" and (self.reason or "").startswith(NOT_LISTED)

    def settling(self, now: float | None = None) -> bool:
        """A ``profile changed`` stale younger than the grace period: held before it asks for a re-Add."""
        if not (self.install_state == "stale" and (self.reason or "").startswith(PROFILE_CHANGED)):
            return False
        age = _age_s(self.stale_at, now)
        return age is not None and age < PROFILE_STALE_GRACE_S

    def wants_add(self, now: float | None = None) -> bool:
        """Does a human need to Add (or re-Add) this printer? New, probed and stale count; an offline
        printer and a settling profile change do not."""
        if self.offline or self.settling(now):
            return False
        return self.install_state != "installed" or self.needs_readd

    def status_text(self, now: float | None = None) -> str:
        """The tray's submenu header: ``installed``, ``needs Add``, ``needs re-Add (profile changed)``,
        ``offline``, ``profile changed, confirming`` or ``waiting in queue``."""
        if self.offline:
            return "offline"
        if self.settling(now):
            return "profile changed, confirming"
        if self.install_state == "stale" or self.needs_readd:
            return "needs re-Add" + (f" ({self.reason})" if self.reason else "")
        if self.install_state == "installed":
            return "installed"
        if not self.announcing and self.queue_position and self.queue_position > 1:
            return "waiting in queue"
        return "needs Add"


def _age_s(ts: str | None, now: float | None) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now if now is not None else datetime.now(timezone.utc).timestamp()) - t.timestamp()


def faults_from_live(live: Mapping[str, Any] | None) -> list[str]:
    """Names of the raised toggles in ``DeviceInfo.live.faults``."""
    f = (live or {}).get("faults") if isinstance(live, Mapping) else None
    if not isinstance(f, Mapping):
        return []
    return [k for k in FAULT_KEYS if f.get(k) is True]


def summary_level(rows: Sequence[StatusRow] | None, now: float | None = None) -> str:
    """The tray icon level: ``green`` (every printer installed, no raised fault), ``amber`` (a printer
    needs an Add or re-Add, or a fault is raised; offline and settling printers do not count as Add
    requests), ``red`` (``rows`` is ``None``: the daemon cannot be read). No printers is green."""
    if rows is None:
        return "red"
    for r in rows:
        if r.wants_add(now) or r.faults:
            return "amber"
    return "green"


def summary_text(rows: Sequence[StatusRow] | None, now: float | None = None) -> str:
    """A tooltip like ``fakeprint: 3 printers, 1 installed, 1 need Add, 1 offline`` (at most 120 chars)."""
    if rows is None:
        return "fakeprint: daemon not reachable"
    n_inst = sum(1 for r in rows if r.install_state == "installed" and not r.needs_readd)
    n_add = sum(1 for r in rows if r.wants_add(now))
    n_off = sum(1 for r in rows if r.offline)
    parts = [f"{len(rows)} printer{'s' if len(rows) != 1 else ''}", f"{n_inst} installed"]
    if n_add:
        parts.append(f"{n_add} need Add")
    if n_off:
        parts.append(f"{n_off} offline")
    return ("fakeprint: " + ", ".join(parts))[:120]


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else (s[: max(n - 1, 0)] + "~" if n > 1 else s[:n])


def format_status_table(rows: Sequence[StatusRow], width: int | None = None) -> str:
    """Plain-text table, at most ``width`` columns wide (default: the terminal's, min 40).

    Columns: ID NAME STATE PROFILE PRINTER JOBS FAULTS CLASS UUID. When too wide, UUID, CLASS and
    then NAME are dropped and the flexible columns are clipped. Printers that need an Add get a
    ``!`` after the state and a guidance-style line underneath.
    """
    if width is None:
        width = shutil.get_terminal_size((100, 24)).columns
    width = max(width, 40)
    if not rows:
        return "(no printers)"

    cols: list[tuple[str, list[str]]] = [
        ("ID", [r.id for r in rows]),
        ("NAME", [r.friendly_name or "-" for r in rows]),
        ("STATE", [r.install_state + ("!" if r.wants_add() else "") for r in rows]),
        ("PROFILE", [r.profile or "-" for r in rows]),
        ("PRINTER", [r.printer_state or "-" for r in rows]),
        ("JOBS", [f"{r.active_jobs}/{r.captured_jobs}" for r in rows]),
        ("FAULTS", [",".join(r.faults) or "-" for r in rows]),
        ("CLASS", [r.klass[:12] or "-" for r in rows]),
        ("UUID", [r.uuid[:8] or "-" for r in rows]),
    ]
    flexible = {"NAME", "PROFILE", "FAULTS"}

    def widths(cs: list[tuple[str, list[str]]]) -> list[int]:
        return [max(len(h), *(len(v) for v in vals)) for h, vals in cs]

    def total(ws: list[int]) -> int:
        return sum(ws) + 2 * (len(ws) - 1)

    for drop in ("UUID", "CLASS", "NAME"):
        if total(widths(cols)) <= width:
            break
        cols = [c for c in cols if c[0] != drop]
    ws = widths(cols)
    while total(ws) > width:
        cand = [i for i, (h, _) in enumerate(cols) if h in flexible and ws[i] > 6]
        if not cand:
            break
        i = max(cand, key=lambda k: ws[k])
        ws[i] -= 1

    def line(cells: list[str]) -> str:
        return "  ".join(_clip(c, w).ljust(w) for c, w in zip(cells, ws)).rstrip()

    out = [line([h for h, _ in cols]), line(["-" * w for w in ws])]
    for i, r in enumerate(rows):
        out.append(line([vals[i] for _, vals in cols]))
        if r.wants_add():
            out.append(_clip(f"  ! {r.id}: {r.status_text()}" + (f" (queue position {r.queue_position})" if r.queue_position else ""), width))
    return "\n".join(out)
