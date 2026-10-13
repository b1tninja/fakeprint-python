"""``fakeprint-lab``: bring the emulated printers up and tell a human what to click. Never Adds anything.

Windows adds one new WSD printer at a time (about 30 seconds each) and nothing in fakeprint may
perform an Add: no ``Add-Printer``, ``Add-PrinterPort`` or ``pnputil`` anywhere. These helpers get the
daemon running (:func:`ensure`), register printers with it (:func:`up`), remove the ones that are not
installed (:func:`down`) and report, one printer at a time, the exact text a person follows in
Settings (:func:`instructions`, :func:`wait`). The daemon's serial queue makes sure only the queue
head announces itself.

>>> mgr = lab.ensure()                                   # doctest: +SKIP
>>> report = lab.up(mgr, ["mono=mono-laser", "color=color-inkjet-duplex"])   # doctest: +SKIP
>>> print(report.text())                                 # doctest: +SKIP
Add now: fakeprint mono (1 of 2). Open Settings > Bluetooth & devices > ...
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import httpx

from . import tray
from .client import DEFAULT_BASE_URL
from .manager import Manager, ManagerError
from .registry import ManagedDevice, QueueInfo
from .ui import add_guidance, format_status_table


@dataclass(frozen=True)
class Spec:
    """A printer to register: ``Spec("mono", "mono-laser")``."""

    id: str
    profile: str
    device_uuid: str | None = None
    friendly_name: str | None = None

    @classmethod
    def parse(cls, text: str) -> Spec:
        """``id=profile``, ``id=profile@uuid`` or just ``profile`` (the id is then the profile id)."""
        head, _, uid = text.partition("@")
        id_, eq, profile = head.partition("=")
        if not eq:
            id_, profile = head, head
        return cls(id_.strip(), profile.strip(), uid.strip() or None)


def _as_spec(x: Any) -> Spec:
    if isinstance(x, Spec):
        return x
    if isinstance(x, str):
        return Spec.parse(x)
    if isinstance(x, dict):
        return Spec(str(x["id"]), str(x.get("profile") or x["id"]), x.get("device_uuid"), x.get("friendly_name"))
    # fakeprint.server.Device (or anything shaped like it)
    spec = getattr(x, "spec", None)
    if callable(spec):
        i, p, u, n = spec()
        return Spec(i, p, u, n)
    return Spec(x.id, x.profile, getattr(x, "device_uuid", None), getattr(x, "friendly_name", None))


@dataclass
class Instruction:
    """What a human must do next, for one printer."""

    device_id: str
    friendly_name: str
    position: int
    total: int
    text: str


@dataclass
class UpReport:
    added: list[str] = field(default_factory=list)       # newly registered with the daemon
    existing: list[str] = field(default_factory=list)    # were already registered (kept as they are)
    failed: dict[str, str] = field(default_factory=dict)  # id -> reason
    instruction: Instruction | None = None                # the ONE printer to Add now (queue head)
    queue: QueueInfo = field(default_factory=QueueInfo)

    def text(self) -> str:
        lines = [f"registered: {', '.join(self.added) or '-'}", f"already present: {', '.join(self.existing) or '-'}"]
        lines += [f"failed {k}: {v}" for k, v in self.failed.items()]
        lines.append(self.instruction.text if self.instruction else "Nothing to Add in Windows.")
        return "\n".join(lines)


@dataclass
class DownReport:
    removed: list[str] = field(default_factory=list)
    kept_installed: list[str] = field(default_factory=list)  # installed printers are left alone
    missing: list[str] = field(default_factory=list)


@dataclass
class WaitReport:
    installed: list[str] = field(default_factory=list)
    timed_out: list[str] = field(default_factory=list)
    shown: list[Instruction] = field(default_factory=list)  # the instructions printed, in order


def ensure(
    base_url: str = DEFAULT_BASE_URL,
    *,
    exe: str | None = None,
    args: Sequence[str] = (),
    opener: Callable[..., Any] | None = None,
    health_check: Callable[[str], bool] | None = None,
    timeout: float = 20.0,
    manager_factory: Callable[[str], Manager] = Manager,
) -> Manager:
    """Attach to the daemon at ``base_url`` or start ``fakeprint --manage`` (attach-first, idempotent,
    never kills anything) and return a :class:`Manager` for it. Starting the daemon does not Add
    any printer."""
    kw: dict[str, Any] = {"timeout": timeout}
    if opener is not None:
        kw["opener"] = opener
    if health_check is not None:
        kw["health_check"] = health_check
    tray.ensure_daemon(base_url, exe, args, **kw)
    return manager_factory(base_url)


def instruction_for(mgr: Manager, queue: QueueInfo | None = None, devices: list[ManagedDevice] | None = None) -> Instruction | None:
    """The one printer a human should Add now (the queue head), with the standard text; None when
    the queue is empty."""
    q = queue or mgr.queue()
    if q.head is None:
        return None
    devs = devices if devices is not None else mgr.devices()
    head = next((d for d in devs if d.id == q.head), None)
    name = head.friendly_name if head and head.friendly_name else q.head
    pos, total = q.position or 1, q.total or 1
    return Instruction(q.head, name, pos, total, add_guidance(name, pos, total))


def up(
    mgr: Manager,
    devices: Iterable[Any] = (),
    *,
    all_profiles: bool = False,
    limit: int | None = None,
) -> UpReport:
    """Register printers with the daemon (``POST /v1/devices``) and report the first Add to do.

    ``devices``: ``Spec``/``"id=profile[@uuid]"``/dicts/:class:`~fakeprint.server.Device`.
    ``all_profiles`` adds one printer per profile the daemon offers (id = profile id), capped by
    ``limit``. Printers already registered are kept untouched; installed printers are never
    modified. Only the queue head announces, so registering many printers cannot flood Windows.
    """
    specs = [_as_spec(d) for d in devices]
    if all_profiles:
        have = {s.id for s in specs}
        extra = [Spec(p.id, p.id) for p in mgr.profiles() if p.id not in have]
        specs += extra
    if limit is not None:
        specs = specs[: max(limit, 0)]
    rep = UpReport()
    present = {d.id for d in mgr.devices()}
    for s in specs:
        if s.id in present:
            rep.existing.append(s.id)
            continue
        try:
            mgr.add(s.id, s.profile, device_uuid=s.device_uuid, friendly_name=s.friendly_name)
            rep.added.append(s.id)
        except ManagerError as e:
            rep.failed[s.id] = str(e)
    rep.queue = mgr.queue()
    rep.instruction = instruction_for(mgr, rep.queue)
    return rep


def down(mgr: Manager, ids: Iterable[str] | None = None, *, forget: bool = False, include_installed: bool = False) -> DownReport:
    """Stop and remove printers (``DELETE /v1/devices/{id}``). Printers that are installed in Windows
    are LEFT ALONE unless ``include_installed`` (the harness never removes installed printers).
    Without ``ids`` every non-installed printer goes."""
    by_id = {d.id: d for d in mgr.devices()}
    wanted = list(ids) if ids is not None else [i for i, d in by_id.items() if d.state != "installed"]
    rep = DownReport()
    for i in wanted:
        d = by_id.get(i)
        if d is None:
            rep.missing.append(i)
        elif d.state == "installed" and not include_installed:
            rep.kept_installed.append(i)
        else:
            mgr.remove(i, forget=forget)
            rep.removed.append(i)
    return rep


def wait(
    mgr: Manager,
    *,
    per_device_timeout: float = 180.0,
    interval: float = 2.0,
    out: Callable[[str], Any] = print,
    reconcile: Callable[[], Any] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> WaitReport:
    """Walk the queue ONE printer at a time: print ``Add 2 of 5: mono now`` plus the standard text,
    wait (bounded by ``per_device_timeout``) until the daemon reports that printer installed, then
    move to the next. Nothing is added by this function: the human clicks Add. ``reconcile`` (for
    example ``mgr.reconcile_from_windows``) is called each poll so a finished Add is noticed; the
    daemon's own detection also works. A printer that times out is recorded and the walk stops
    there (it does not skip or rotate anything on its own)."""
    rep = WaitReport()
    seen_installed: set[str] = set()
    while True:
        q = mgr.queue()
        if q.head is None:
            return rep
        head = q.head
        if head in seen_installed:  # installed, daemon still settling: wait for the promotion
            sleep(interval)
            continue
        ins = instruction_for(mgr, q)
        assert ins is not None
        out(f"Add {ins.position} of {ins.total}: {ins.device_id} now")
        out(ins.text)
        rep.shown.append(ins)
        deadline = clock() + per_device_timeout
        while True:
            if reconcile is not None:
                try:
                    reconcile()
                except Exception as e:  # reading Windows is best effort; the daemon may detect it itself
                    out(f"(reconcile failed: {e})")
            if mgr.device(head).state == "installed":
                rep.installed.append(head)
                seen_installed.add(head)
                break
            if clock() >= deadline:
                rep.timed_out.append(head)
                out(f"{head}: not installed after {per_device_timeout:.0f}s; stopping here")
                return rep
            sleep(interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fakeprint-lab", description="fakeprint lab: printers up/down, Add guidance (never adds anything)")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    sub = p.add_subparsers(dest="cmd", required=True)

    en = sub.add_parser("ensure", help="attach to the daemon or start `fakeprint --manage` (never kills)")
    en.add_argument("--exe", default=None)
    en.add_argument("arg", nargs="*", help="extra arguments for the daemon (after --)")

    sub.add_parser("status", help="table of printers (state, install state, faults)")
    sub.add_parser("queue", help="the serial install queue")
    sub.add_parser("guidance", help="what to click for the printer at the queue head")

    u = sub.add_parser("up", help="register printers; reports the first one to Add")
    u.add_argument("device", nargs="*", help="id=profile[@uuid] (or just a profile id)")
    u.add_argument("--all-profiles", action="store_true", help="one printer per profile the daemon offers")
    u.add_argument("--limit", type=int, default=None)

    d = sub.add_parser("down", help="remove printers (installed ones are kept unless --include-installed)")
    d.add_argument("id", nargs="*")
    d.add_argument("--forget", action="store_true", help="also drop the registry row")
    d.add_argument("--include-installed", action="store_true")

    w = sub.add_parser("wait", help="walk the queue one printer at a time until each is installed")
    w.add_argument("--timeout", type=float, default=180.0, help="seconds per printer")
    w.add_argument("--interval", type=float, default=2.0)

    sub.add_parser("open-settings", help="open Windows Printers & scanners (a human then clicks Add)")
    sub.add_parser("reconcile", help="list Windows printers read-only and reconcile with the daemon")

    args = p.parse_args(argv)
    try:
        if args.cmd == "ensure":
            mode = tray.ensure_daemon(args.base_url, args.exe, args.arg)
            print(f"daemon {mode} at {args.base_url}")
            return 0
        with Manager(args.base_url) as mgr:
            if args.cmd == "status":
                print(format_status_table(mgr.status_table()))
                print(f"level: {mgr.summary_level()}")
            elif args.cmd == "queue":
                q = mgr.queue()
                print(f"head={q.head} remaining={q.remaining} total={q.total}")
            elif args.cmd == "guidance":
                print(mgr.guidance() or "Nothing to Add in Windows.")
            elif args.cmd == "up":
                print(up(mgr, args.device, all_profiles=args.all_profiles, limit=args.limit).text())
            elif args.cmd == "down":
                r = down(mgr, args.id or None, forget=args.forget, include_installed=args.include_installed)
                print(f"removed: {', '.join(r.removed) or '-'}; kept (installed): {', '.join(r.kept_installed) or '-'}; unknown: {', '.join(r.missing) or '-'}")
            elif args.cmd == "wait":
                r = wait(mgr, per_device_timeout=args.timeout, interval=args.interval, reconcile=mgr.reconcile_from_windows)
                print(f"installed: {', '.join(r.installed) or '-'}; timed out: {', '.join(r.timed_out) or '-'}")
                return 1 if r.timed_out else 0
            elif args.cmd == "open-settings":
                print(mgr.open_add_device_settings())
            elif args.cmd == "reconcile":
                r = mgr.reconcile_from_windows()
                print(f"installed: {r.installed}; stale: {r.stale}; foreign: {[w.name for w in r.foreign]}")
    except (ManagerError, httpx.HTTPError, OSError, RuntimeError, TimeoutError, ValueError) as e:
        print(f"fakeprint-lab: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
