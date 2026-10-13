"""Typed, read-only view of the fakeprint device registry (``devices.json``) and the matching rules.

Python NEVER writes the registry file. Every mutation goes through the daemon
(:class:`fakeprint.manager.Manager`), so the Rust file lock and atomic writer stay the single writer.

* :func:`load` reads ``%LOCALAPPDATA%\\fakeprint\\devices.json`` directly (read-only, tolerant of a
  concurrent atomic rewrite); :func:`parse` takes a decoded document; :func:`from_server` asks a
  daemon (``GET /v1/registry``).
* :func:`match_printers_to_devices` is the contract's rule (docs/multi-device.md 3.4): a Windows
  printer belongs to a device only when (a) its port string holds exactly one distinct UUID and it is the
  device's WSD UUID (``kind="wsd"``), or (b) its port is an ``http(s)``/``ipp(s)`` URL on the device's
  own authority and exactly its IPP path (``kind="ipp"``). Everything else (a TCPMON ``host:9100``
  port, names that merely look alike, two UUIDs in a port, an unknown UUID) is *foreign*.
* :func:`reconcile` applies that rule to a listing without changing anything.
* :func:`capability_class` and :func:`select_installed` are the harness helpers: pick "an installed
  printer of this shape" by the 12-hex-digit class of the published capability fingerprint.

Parsing is lenient: unknown fields are kept in ``.extra``; a legacy single-row file that only has
``added.pnp`` loads as ``installed`` with the note ``"legacy added.pnp"``.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

REGISTRY_ENV = "FAKEPRINT_REGISTRY"
LEGACY_NOTE = "legacy added.pnp"
NOT_LISTED = "not listed by Windows"
PROFILE_CHANGED = "profile changed"

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_DEFAULT_PORTS = {"http": 80, "https": 443, "ipp": 631, "ipps": 631}


def registry_path() -> Path:
    """Same rules as ``registry.rs``: ``$FAKEPRINT_REGISTRY``, else
    ``%LOCALAPPDATA%\\fakeprint\\devices.json`` (Windows) or ``~/.local/share/fakeprint/devices.json``."""
    env = os.environ.get(REGISTRY_ENV)
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or "C:\\Users\\Default\\AppData\\Local"
        return Path(base) / "fakeprint" / "devices.json"
    home = os.environ.get("HOME") or "/tmp"
    return Path(home) / ".local" / "share" / "fakeprint" / "devices.json"


def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _split(raw: Mapping[str, Any], known: Iterable[str]) -> dict[str, Any]:
    k = set(known)
    return {key: val for key, val in raw.items() if key not in k}


# ---------------------------------------------------------------------------------------------
# Typed documents
# ---------------------------------------------------------------------------------------------


@dataclass
class WindowsPrinter:
    """A Windows printer as observed on the Windows side (input to ``installed``/``reconcile``)."""

    name: str = ""
    port: str = ""
    driver: str = ""
    id: str = ""

    @classmethod
    def parse(cls, raw: Any) -> WindowsPrinter:
        """From a mapping (``name``/``port``/``driver``/``id``) or any object with those attributes
        (for example :class:`fakeprint.winprint.PrinterInfo`)."""
        if isinstance(raw, WindowsPrinter):
            return raw
        get = raw.get if isinstance(raw, Mapping) else (lambda k, d=None: getattr(raw, k, d))
        return cls(
            name=str(get("name") or ""),
            port=str(get("port") or ""),
            driver=str(get("driver") or ""),
            id=str(get("id") or ""),
        )

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "port": self.port, "driver": self.driver, "id": self.id}


@dataclass
class Summary:
    """The capability summary of a config (``config.summary``)."""

    formats: list[str] = field(default_factory=list)
    color_modes: list[str] = field(default_factory=list)
    sides: list[str] = field(default_factory=list)
    media_sizes: list[str] = field(default_factory=list)
    input_bins: list[str] = field(default_factory=list)
    versions: dict[str, bool] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Summary:
        r = _d(raw)
        lists = ("formats", "color_modes", "sides", "media_sizes", "input_bins")
        return cls(
            **{k: [str(x) for x in (r.get(k) or [])] for k in lists},
            versions={str(k): bool(v) for k, v in _d(r.get("versions")).items()},
            extra=_split(r, (*lists, "versions")),
        )


@dataclass
class Config:
    """What a device was when last started (``config`` of a registry row / ``DeviceInfo``)."""

    fingerprint: str = ""
    published_fingerprint: str = ""
    klass: str = ""
    summary: Summary = field(default_factory=Summary)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Config | None:
        if not isinstance(raw, dict):
            return None
        published = str(raw.get("published_fingerprint") or "")
        return cls(
            fingerprint=str(raw.get("fingerprint") or ""),
            published_fingerprint=published,
            klass=str(raw.get("class") or capability_class(published) or ""),
            summary=Summary.parse(raw.get("summary")),
            extra=_split(raw, ("fingerprint", "published_fingerprint", "class", "summary")),
        )


_INSTALL_KEYS = (
    "state", "reason", "note", "first_probed_at", "last_seen_at", "last_peer", "installed_at",
    "verified_at", "kind", "windows", "published_fingerprint_at_install", "stale_from", "stale_at",
)


@dataclass
class Install:
    """Install lifecycle: ``new | probed | installed | stale``."""

    state: str = "new"
    reason: str | None = None
    note: str | None = None
    first_probed_at: str | None = None
    last_seen_at: str | None = None
    last_peer: str | None = None
    installed_at: str | None = None
    verified_at: str | None = None
    kind: str | None = None
    windows: WindowsPrinter | None = None
    published_fingerprint_at_install: str | None = None
    stale_from: str | None = None
    stale_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Install:
        r = _d(raw)
        win = r.get("windows")
        has_win = isinstance(win, dict) and any(win.values())
        return cls(
            state=str(r.get("state") or "new"),
            reason=_s(r.get("reason")),
            note=_s(r.get("note")),
            first_probed_at=_s(r.get("first_probed_at")),
            last_seen_at=_s(r.get("last_seen_at")),
            last_peer=_s(r.get("last_peer")),
            installed_at=_s(r.get("installed_at")),
            verified_at=_s(r.get("verified_at")),
            kind=_s(r.get("kind")),
            windows=WindowsPrinter.parse(win) if has_win else None,
            published_fingerprint_at_install=_s(r.get("published_fingerprint_at_install")),
            stale_from=_s(r.get("stale_from")),
            stale_at=_s(r.get("stale_at")),
            extra=_split(r, _INSTALL_KEYS),
        )

    @property
    def installed(self) -> bool:
        return self.state == "installed"

    @property
    def stale(self) -> bool:
        return self.state == "stale"

    @property
    def offline(self) -> bool:
        """Stale because Windows no longer lists it: offline, not a request to Add again."""
        return self.stale and (self.reason or "").startswith(NOT_LISTED)

    @property
    def profile_changed(self) -> bool:
        return self.stale and (self.reason or "").startswith(PROFILE_CHANGED)


def _needs_readd(install: Install) -> tuple[bool, str | None]:
    # Mirrors the daemon: a previously installed device that went stale needs a re-Add.
    if install.stale:
        return True, install.reason
    return False, None


@dataclass
class DeviceRow:
    """One registry row (one emulated printer)."""

    name: str
    listen: str = ""
    advertise: str = ""
    device_uuid: str = ""
    ipp_uuid: str | None = None
    profile: str = "generic-wsd"
    friendly_name: str | None = None
    discovery: bool = False
    jetdirect_port: int | None = None
    config: Config | None = None
    install: Install = field(default_factory=Install)
    exe: str | None = None
    autostart: bool = False
    harness: Any = None
    added_pnp: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> DeviceRow:
        install = Install.parse(raw.get("install"))
        pnp = bool(_d(raw.get("added")).get("pnp", False))
        if pnp and install.state == "new":  # old single-row format: only the boolean existed
            install.state = "installed"
            install.note = install.note or LEGACY_NOTE
        jp = raw.get("jetdirect_port")
        return cls(
            name=str(raw.get("name") or ""),
            listen=str(raw.get("listen") or ""),
            advertise=str(raw.get("advertise") or ""),
            device_uuid=str(raw.get("device_uuid") or ""),
            ipp_uuid=_s(raw.get("ipp_uuid")),
            profile=str(raw.get("profile") or "generic-wsd"),
            friendly_name=_s(raw.get("friendly_name")),
            discovery=bool(raw.get("discovery", False)),
            jetdirect_port=int(jp) if isinstance(jp, int) and not isinstance(jp, bool) else None,
            config=Config.parse(raw.get("config")),
            install=install,
            exe=_s(raw.get("exe")),
            autostart=bool(raw.get("autostart", False)),
            harness=raw.get("harness"),
            added_pnp=pnp,
            extra=_split(
                raw,
                ("name", "listen", "advertise", "device_uuid", "ipp_uuid", "profile", "friendly_name", "discovery",
                 "jetdirect_port", "config", "install", "exe", "autostart", "harness", "added"),
            ),
        )

    @property
    def id(self) -> str:
        return self.name

    @property
    def uuid(self) -> str:
        return self.device_uuid

    @property
    def needs_readd(self) -> bool:
        return _needs_readd(self.install)[0]

    @property
    def needs_readd_reason(self) -> str | None:
        return _needs_readd(self.install)[1]

    @property
    def klass(self) -> str:
        return self.config.klass if self.config else ""


@dataclass
class Registry:
    """The registry document."""

    devices: list[DeviceRow] = field(default_factory=list)
    advertise_default: str | None = None
    port_policy: dict[str, Any] = field(default_factory=dict)
    queue_order: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Registry:
        r = _d(raw)
        rows = r.get("devices")
        return cls(
            devices=[DeviceRow.parse(x) for x in rows if isinstance(x, dict)] if isinstance(rows, list) else [],
            advertise_default=_s(r.get("advertise_default")),
            port_policy=_d(r.get("port_policy")),
            queue_order=[str(x) for x in (r.get("queue_order") or [])],
            extra=_split(r, ("devices", "advertise_default", "port_policy", "queue_order")),
        )

    def find(self, name: str) -> DeviceRow | None:
        return next((d for d in self.devices if d.name == name), None)

    def rows_for_listen(self, listen: str) -> list[DeviceRow]:
        return [d for d in self.devices if d.listen == listen]


@dataclass
class ManagedDevice:
    """One element of ``GET /v1/devices`` (a running printer plus its registry state)."""

    id: str
    uuid: str
    profile: str
    ipp_uuid: str | None = None
    friendly_name: str = ""
    default: bool = False
    base_path: str = ""
    device_endpoint: str | None = None
    service_endpoint: str | None = None
    ipp_endpoint: str | None = None
    jetdirect_port: int | None = None
    inbox_dir: str = ""
    announcing: bool = False
    queue_position: int | None = None
    needs_readd: bool = False
    needs_readd_reason: str | None = None
    install: Install | None = None
    config: Config | None = None
    live: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> ManagedDevice:
        inst = raw.get("install")
        qp = raw.get("queue_position")
        jp = raw.get("jetdirect_port")
        return cls(
            id=str(raw.get("id") or ""),
            uuid=str(raw.get("uuid") or ""),
            profile=str(raw.get("profile") or ""),
            ipp_uuid=_s(raw.get("ipp_uuid")),
            friendly_name=str(raw.get("friendly_name") or ""),
            default=bool(raw.get("default", False)),
            base_path=str(raw.get("base_path") or ""),
            device_endpoint=_s(raw.get("device_endpoint")),
            service_endpoint=_s(raw.get("service_endpoint")),
            ipp_endpoint=_s(raw.get("ipp_endpoint")),
            jetdirect_port=int(jp) if isinstance(jp, int) and not isinstance(jp, bool) else None,
            inbox_dir=str(raw.get("inbox_dir") or ""),
            announcing=bool(raw.get("announcing", False)),
            queue_position=int(qp) if isinstance(qp, int) and not isinstance(qp, bool) else None,
            needs_readd=bool(raw.get("needs_readd", False)),
            needs_readd_reason=_s(raw.get("needs_readd_reason")),
            install=Install.parse(inst) if isinstance(inst, dict) else None,
            config=Config.parse(raw.get("config")),
            live=_d(raw.get("live")),
            extra=_split(
                raw,
                ("id", "uuid", "profile", "ipp_uuid", "friendly_name", "default", "base_path", "device_endpoint",
                 "service_endpoint", "ipp_endpoint", "jetdirect_port", "inbox_dir", "announcing", "queue_position",
                 "needs_readd", "needs_readd_reason", "install", "config", "live"),
            ),
        )

    @property
    def state(self) -> str:
        return self.install.state if self.install else "new"

    @property
    def device_uuid(self) -> str:
        return self.uuid

    @property
    def klass(self) -> str:
        return self.config.klass if self.config else ""

    @property
    def installed(self) -> bool:
        return self.state == "installed"


@dataclass
class QueueInfo:
    """``GET /v1/queue``: the serial install queue (one Add at a time)."""

    head: str | None = None
    remaining: list[str] = field(default_factory=list)
    position: int | None = None
    total: int = 0
    head_since: str | None = None
    head_timeout_s: int = 180
    settle_s: int = 5
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> QueueInfo:
        r = _d(raw)
        pos = r.get("position")
        return cls(
            head=_s(r.get("head")),
            remaining=[str(x) for x in (r.get("remaining") or [])],
            position=int(pos) if isinstance(pos, int) and not isinstance(pos, bool) else None,
            total=int(r.get("total") or 0),
            head_since=_s(r.get("head_since")),
            head_timeout_s=int(r.get("head_timeout_s") or 180),
            settle_s=int(r.get("settle_s") or 5),
            extra=_split(r, ("head", "remaining", "position", "total", "head_since", "head_timeout_s", "settle_s")),
        )

    @property
    def empty(self) -> bool:
        return self.head is None

    @property
    def ids(self) -> list[str]:
        """Head first, then the rest, in install order."""
        return ([self.head] if self.head else []) + list(self.remaining)


@dataclass
class ReconcileResult:
    """``POST /v1/reconcile`` answer (or the local :func:`reconcile`)."""

    installed: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    offline: list[str] = field(default_factory=list)
    foreign: list[WindowsPrinter] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> ReconcileResult:
        return cls(
            installed=[str(x) for x in raw.get("installed") or []],
            stale=[str(x) for x in raw.get("stale") or []],
            offline=[str(x) for x in raw.get("offline") or []],
            foreign=[WindowsPrinter.parse(x) for x in raw.get("foreign") or [] if isinstance(x, dict)],
            extra=_split(raw, ("installed", "stale", "offline", "foreign")),
        )


# ---------------------------------------------------------------------------------------------
# Matching (docs/multi-device.md 3.4)
# ---------------------------------------------------------------------------------------------


def port_uuids(port: str) -> set[str]:
    """Distinct lowercase UUIDs in a port string (braces and ``urn:uuid:`` are irrelevant)."""
    return {m.group(0).lower() for m in _UUID_RE.finditer(port or "")}


def _norm_uuid(u: str) -> str:
    return str(u).strip().lower().replace("urn:uuid:", "").strip("{}")


def _authority(url: str) -> tuple[str, int] | None:
    """``(host, port)`` of an http/https/ipp/ipps URL (default port filled in), else None."""
    try:
        sp = urlsplit(url.strip())
        scheme = sp.scheme.lower()
        if scheme not in _DEFAULT_PORTS or not sp.hostname:
            return None
        return sp.hostname.lower(), sp.port or _DEFAULT_PORTS[scheme]
    except ValueError:
        return None


def _url_path(url: str) -> str:
    try:
        return urlsplit(url.strip()).path
    except ValueError:
        return ""


def _dev_attr(dev: Any, *names: str) -> str:
    for n in names:
        v = dev.get(n) if isinstance(dev, Mapping) else getattr(dev, n, None)
        if v:
            return str(v)
    return ""


def match_kind(dev: Any, printer: WindowsPrinter | Mapping[str, Any]) -> str | None:
    """``"wsd"``, ``"ipp"`` or ``None``: does this Windows printer's port prove it is ``dev``?

    ``dev``: a :class:`ManagedDevice`, :class:`DeviceRow`, :class:`~fakeprint.server.Device` or dict
    (needs ``uuid``/``device_uuid`` and, for the IPP rule, ``ipp_endpoint``).
    """
    p = WindowsPrinter.parse(printer)
    uid = _norm_uuid(_dev_attr(dev, "uuid", "device_uuid"))
    found = port_uuids(p.port)
    if uid and len(found) == 1 and next(iter(found)) == uid:
        return "wsd"
    ipp = _dev_attr(dev, "ipp_endpoint")
    if ipp and not found:
        a, b = _authority(p.port), _authority(ipp)
        if a and b and a == b and _url_path(p.port) == _url_path(ipp):
            return "ipp"
    return None


@dataclass
class PrinterMatch:
    """Result of :func:`match_printers_to_devices`."""

    matched: dict[str, WindowsPrinter] = field(default_factory=dict)  # device id -> its Windows printer
    kinds: dict[str, str] = field(default_factory=dict)               # device id -> "wsd" | "ipp"
    foreign: list[WindowsPrinter] = field(default_factory=list)       # not provably a registered device
    duplicates: list[WindowsPrinter] = field(default_factory=list)    # a 2nd printer for an already matched device


def match_printers_to_devices(
    devices: Iterable[Any], printers: Iterable[Mapping[str, Any] | WindowsPrinter | Any]
) -> PrinterMatch:
    """Classify a Windows printer listing against devices with the contract's rule (see module doc).
    Nothing is changed anywhere; a name that merely looks like a fakeprint name never matches."""
    devs = [d for d in devices if _dev_attr(d, "id", "name")]
    out = PrinterMatch()
    for raw in printers:
        p = WindowsPrinter.parse(raw)
        hit: tuple[str, str] | None = None
        for d in devs:
            kind = match_kind(d, p)
            if kind:
                hit = (_dev_attr(d, "id", "name"), kind)
                break
        if hit is None:
            out.foreign.append(p)
        elif hit[0] in out.matched:
            out.duplicates.append(p)
        else:
            out.matched[hit[0]] = p
            out.kinds[hit[0]] = hit[1]
    return out


def reconcile(devices: Iterable[Any], printers: Iterable[Any]) -> ReconcileResult:
    """The local, read-only reconcile: which devices Windows has (``installed``), which installed
    devices it no longer lists (``stale``, reason ``not listed by Windows``: offline, not an Add
    request) and which printers are foreign. The daemon does the same on ``POST /v1/reconcile``
    and is the one that records it."""
    devs = list(devices)
    m = match_printers_to_devices(devs, printers)
    stale = [
        _dev_attr(d, "id", "name")
        for d in devs
        if _dev_attr(d, "id", "name") not in m.matched and _state_of(d) == "installed"
    ]
    return ReconcileResult(installed=list(m.matched), stale=stale, offline=list(stale), foreign=m.foreign + m.duplicates)


def _state_of(dev: Any) -> str:
    inst = dev.get("install") if isinstance(dev, Mapping) else getattr(dev, "install", None)
    if isinstance(inst, Mapping):
        return str(inst.get("state") or "new")
    if inst is not None:
        return str(getattr(inst, "state", "new"))
    return str(getattr(dev, "state", "new"))


# ---------------------------------------------------------------------------------------------
# Capability class (harness helpers)
# ---------------------------------------------------------------------------------------------


def capability_class(published_fingerprint: str | None) -> str:
    """The class handle of a published fingerprint (``fnv64:<hex>``): its first 12 hex digits.
    Empty for an empty/malformed fingerprint."""
    fp = (published_fingerprint or "").strip().lower()
    if fp.startswith("fnv64:"):
        fp = fp[len("fnv64:"):]
    return fp[:12] if re.fullmatch(r"[0-9a-f]{12,}", fp) else ""


def device_class(dev: Any) -> str:
    """The class of a device-like object (``config.class`` or derived from the published fingerprint)."""
    cfg = dev.get("config") if isinstance(dev, Mapping) else getattr(dev, "config", None)
    if cfg is None:
        return str(getattr(dev, "klass", "") or "")
    get = cfg.get if isinstance(cfg, Mapping) else (lambda k, d=None: getattr(cfg, k, d))
    return str(get("class") or getattr(cfg, "klass", "") or capability_class(get("published_fingerprint")) or "")


def classes(devices: Iterable[Any]) -> dict[str, list[str]]:
    """``{class: [device ids]}`` for the given devices (devices without a class are left out)."""
    out: dict[str, list[str]] = {}
    for d in devices:
        c = device_class(d)
        if c:
            out.setdefault(c, []).append(_dev_attr(d, "id", "name"))
    return out


def select_installed(devices: Iterable[Any], klass: str | None = None, profile: str | None = None) -> list[Any]:
    """Installed, not-stale devices of a capability class (and/or profile), registry order."""
    return [
        d
        for d in devices
        if _state_of(d) == "installed"
        and (klass is None or device_class(d) == klass)
        and (profile is None or _dev_attr(d, "profile") == profile)
    ]


def skip_message(devices: Iterable[Any], klass: str | None = None, profile: str | None = None) -> str:
    """The text of the skip a harness reports when no installed printer fits: names the printers that
    would (and what a human must do), or says that none exist. Nothing is added automatically."""
    want = f"class {klass}" if klass else f"profile {profile}"
    cand = [
        d for d in devices
        if (klass is None or device_class(d) == klass) and (profile is None or _dev_attr(d, "profile") == profile)
    ]
    if not cand:
        return f"no fakeprint printer of {want} is registered; add one in the device manager, then Add it in Windows"
    names = ", ".join(f"{_dev_attr(d, 'friendly_name', 'id', 'name')} ({_state_of(d)})" for d in cand)
    return (
        f"no installed fakeprint printer of {want}; candidates: {names}. "
        "Add it in Windows (Settings > Bluetooth & devices > Printers & scanners > Add device); "
        "nothing is added automatically"
    )


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------


def parse(doc: Any) -> Registry:
    """Parse an already-decoded registry document (e.g. the body of ``GET /v1/registry``)."""
    return Registry.parse(doc)


def load(path: str | os.PathLike[str] | None = None, *, retries: int = 6, delay: float = 0.05) -> Registry:
    """Read the registry file (read-only). A missing file is an empty registry.

    Writers replace the file atomically, but on Windows a reader can hit a transient sharing
    violation or (rarely) a partial view, so transient OS/JSON errors are retried. The
    ``devices.json.lock`` file is never opened or held by readers.
    """
    p = Path(path) if path is not None else registry_path()
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            text = p.read_text(encoding="utf-8-sig")
            return Registry.parse(json.loads(text) if text.strip() else {})
        except FileNotFoundError:
            return Registry()
        except (OSError, ValueError) as e:  # PermissionError, sharing violation, torn JSON
            last = e
            time.sleep(delay * (attempt + 1))
    raise OSError(f"could not read registry {p}: {last}") from last


def from_server(client: Any) -> Registry:
    """Read the registry through a daemon: ``GET /v1/registry`` (``client``: Manager, URL or anything
    a Manager accepts)."""
    from .manager import Manager

    if isinstance(client, Manager):
        return client.registry()
    with Manager(client) as m:
        return m.registry()


__all__ = [
    "Config", "DeviceRow", "Install", "ManagedDevice", "PrinterMatch", "QueueInfo", "ReconcileResult", "Registry",
    "Summary", "WindowsPrinter", "capability_class", "classes", "device_class", "from_server", "load",
    "match_kind", "match_printers_to_devices", "parse", "port_uuids", "reconcile", "registry_path",
    "select_installed", "skip_message",
]
