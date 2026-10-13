"""``fakeprint-ctl``: command-line control of a running fakeprint (HTTP or named pipe).

    fakeprint-ctl status [--raw] [--width N]       table of printers (--raw: ctl info + devices JSON)
    fakeprint-ctl devices [--width N]              same table
    fakeprint-ctl ui                               open <base-url>/ui in the browser
    fakeprint-ctl profiles                         GET /v1/profiles
    fakeprint-ctl add ID --profile P [--uuid U] [--name FRIENDLY] [--jetdirect-port N]
    fakeprint-ctl remove ID [--forget]
    fakeprint-ctl queue [--advance | --skip | --order ID ...]
    fakeprint-ctl levels ID [--consumable NAME=LEVEL ...] [--input NAME=LEVEL ...]
    fakeprint-ctl faults ID [--paper-out BOOL] [--toner-low BOOL] [--cover-open BOOL] [--paper-jam BOOL]
                         [--offline BOOL] [--clear]
    fakeprint-ctl reset ID
    fakeprint-ctl open-settings                    open Windows Printers & scanners (a human clicks Add)
    fakeprint-ctl ctl CMD [--args JSON]            any ctl command

``--pipe NAME`` talks to ``\\\\.\\pipe\\fakeprint-NAME`` instead of HTTP (Windows). Nothing here adds
a printer in Windows, and nothing writes the registry file: the daemon does.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from .client import DEFAULT_BASE_URL
from .manager import Manager, ManagerError
from .ui import format_status_table

_FAULT_FLAGS = {
    "paper_out": "paper_out", "toner_low": "toner_low", "cover_open": "cover_open",
    "paper_jam": "paper_jam", "offline": "not_accepting_jobs",
}


def _bool(s: str) -> bool:
    v = s.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected bool, got {s!r}")


def _kv(s: str) -> tuple[str, int]:
    k, eq, v = s.partition("=")
    if not eq or not k:
        raise argparse.ArgumentTypeError(f"expected NAME=LEVEL, got {s!r}")
    return k, int(v)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fakeprint-ctl", description="fakeprint control CLI (never adds a printer in Windows)")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--pipe", default=None, metavar="NAME", help="use the named pipe fakeprint-NAME instead of HTTP")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "devices"):
        s = sub.add_parser(name)
        s.add_argument("--raw", action="store_true")
        s.add_argument("--width", type=int, default=None)
    sub.add_parser("ui")
    sub.add_parser("profiles")
    a = sub.add_parser("add")
    a.add_argument("id")
    a.add_argument("--profile", required=True)
    a.add_argument("--uuid", default=None)
    a.add_argument("--name", default=None)
    a.add_argument("--jetdirect-port", type=int, default=None)
    r = sub.add_parser("remove")
    r.add_argument("id")
    r.add_argument("--forget", action="store_true")
    q = sub.add_parser("queue")
    g = q.add_mutually_exclusive_group()
    g.add_argument("--advance", action="store_true")
    g.add_argument("--skip", action="store_true")
    g.add_argument("--order", nargs="+", metavar="ID")
    lv = sub.add_parser("levels")
    lv.add_argument("id")
    lv.add_argument("--consumable", action="append", type=_kv, default=[], metavar="NAME=LEVEL")
    lv.add_argument("--input", action="append", type=_kv, default=[], metavar="NAME=LEVEL")
    f = sub.add_parser("faults")
    f.add_argument("id")
    for flag in _FAULT_FLAGS:
        f.add_argument("--" + flag.replace("_", "-"), type=_bool, default=None)
    f.add_argument("--clear", action="store_true", help="switch every fault off")
    rs = sub.add_parser("reset")
    rs.add_argument("id")
    sub.add_parser("open-settings")
    c = sub.add_parser("ctl")
    c.add_argument("ctl_cmd", metavar="CMD")
    c.add_argument("--args", default="{}")
    return p


def _dump(x: Any) -> None:
    print(json.dumps(x, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mgr = Manager.pipe(args.pipe) if args.pipe else Manager(args.base_url)
    try:
        with mgr:
            return _run(args, mgr)
    except ManagerError as e:
        print(f"fakeprint-ctl: {e}", file=sys.stderr)
        return 1
    except (httpx.HTTPError, OSError, ValueError, KeyError) as e:
        print(f"fakeprint-ctl: {e}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, mgr: Manager) -> int:
    cmd = args.cmd
    if cmd in ("status", "devices"):
        if getattr(args, "raw", False):
            _dump(mgr._call("GET", "/v1/devices", ctl=("devices", {})))
            return 0
        print(format_status_table(mgr.status_table(), args.width))
        print(f"level: {mgr.summary_level()}")
    elif cmd == "ui":
        print(mgr.open_ui())
    elif cmd == "profiles":
        for pr in mgr.profiles():
            print(f"{pr.id}\t{','.join(pr.formats)}\t{pr.description}")
    elif cmd == "add":
        d = mgr.add(args.id, args.profile, device_uuid=args.uuid, friendly_name=args.name, jetdirect_port=args.jetdirect_port)
        print(f"added {d.id} uuid={d.uuid} state={d.state}")
    elif cmd == "remove":
        mgr.remove(args.id, forget=args.forget)
        print(f"removed {args.id}")
    elif cmd == "queue":
        q = mgr.advance() if args.advance else mgr.skip() if args.skip else mgr.reorder(args.order) if args.order else mgr.queue()
        print(f"head={q.head} remaining={q.remaining} total={q.total}")
        g = mgr.guidance() if q.head else None
        if g:
            print(g)
    elif cmd == "levels":
        _dump(mgr.levels(args.id, dict(args.consumable), dict(args.input)))
    elif cmd == "faults":
        body: dict[str, Any] = {"reset": True} if args.clear else {}
        for k, wire in _FAULT_FLAGS.items():
            v = getattr(args, k)
            if v is not None:
                body[wire] = v
        if not body:
            print("nothing to set (use --paper-out, --toner-low, ... or --clear)", file=sys.stderr)
            return 2
        _dump(mgr.ctl("faults", device=args.id, **body))
    elif cmd == "reset":
        _dump(mgr.ctl("reset", device=args.id))
    elif cmd == "open-settings":
        print(mgr.open_add_device_settings())
    elif cmd == "ctl":
        _dump(mgr.ctl(args.ctl_cmd, **json.loads(args.args)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
