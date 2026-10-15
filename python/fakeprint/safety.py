"""Test-harness device protection for fakeprint users: a thin adapter over :mod:`fakescan.safety`.

The policy, the guards and the pytest plugin live in ``fakescan.safety`` / ``fakescan.safety_pytest`` (one
implementation for both emulators; pure Python, no Rust). This module re-exports them and adds the fakeprint
specifics: turning a ``Server`` / ``Device`` / ``Status`` into a device reference, and a ``PrintClient`` factory that
checks the endpoint first.

``fakescan`` is a hard requirement here, on purpose: a guard that silently falls back to "no guard" is worse than an
import error. Install it with ``pip install fakeprint[testpage]`` or put ``fakescan`` on the path.
"""

from __future__ import annotations

from typing import Any

try:
    from fakescan.safety import (  # noqa: F401
        ALLOW_REAL_ENV,
        FAKE_UUID_PREFIX,
        LEGACY_DENY_ENVS,
        POLICY_ENV,
        DeviceNotAllowed,
        Installation,
        Marked,
        MarkedListing,
        Policy,
        PolicyError,
        Verdict,
        allowed,
        assert_fake,
        assert_fake_queue,
        current_policy,
        filter_refs,
        guard,
        guard_open,
        guard_submit,
        guarded,
        install,
        is_fake,
        normalise_uuid,
        queue_ref,
        self_check,
        set_policy,
        use_policy,
        uuids_in,
    )
except ImportError as exc:  # pragma: no cover - environment
    raise ImportError(
        "fakeprint.safety needs fakescan.safety (the shared test-harness device guard); "
        "install fakescan (pip install fakeprint[testpage]) or add it to PYTHONPATH"
    ) from exc

import fakescan.safety as _shared  # noqa: E402


def __getattr__(name: str) -> Any:
    """Everything else (``SINKS``, ``PURE``, ``Sink``, ``is_covered``, ``AUTO`` ...) is read live from the shared module."""
    return getattr(_shared, name)


__all__ = [
    "ALLOW_REAL_ENV", "DeviceNotAllowed", "FAKE_UUID_PREFIX", "Installation", "LEGACY_DENY_ENVS", "Marked",
    "MarkedListing", "POLICY_ENV", "Policy", "PolicyError", "Verdict", "allowed", "assert_fake", "assert_fake_queue", "current_policy",
    "device_ref", "filter_refs", "guard", "guard_open", "guard_submit", "guarded", "guarded_print_client", "install",
    "is_fake", "normalise_uuid", "queue_ref", "self_check", "set_policy", "use_policy", "uuids_in",
]


def device_ref(obj: Any) -> dict[str, Any]:
    """The policy reference of a fakeprint ``Server`` / ``Device`` / ``Status`` / control-plane device row: its device
    uuid (the identity), its endpoints and its name. Missing parts are left out, which makes the reference
    unprovable (excluded) rather than wrong."""
    if isinstance(obj, dict):
        src = obj
        get = src.get
    else:
        def get(k: str, default: Any = None) -> Any:
            try:
                return getattr(obj, k, default)
            except Exception:  # noqa: BLE001
                return default

    # a Server has the default device; its own attributes carry the endpoints
    dev = get("default_device") if not isinstance(obj, dict) else None
    ref: dict[str, Any] = {}
    uuid = get("device_uuid") or get("uuid") or (getattr(dev, "uuid", None) if dev is not None else None)
    if uuid:
        ref["uuid"] = uuid
    for key in ("service_endpoint", "device_endpoint", "ipp_endpoint"):
        v = get(key) or (getattr(dev, key, None) if dev is not None else None)
        if v:
            ref[key] = v
    name = get("friendly_name") or get("name")
    if isinstance(name, str) and name:
        ref["name"] = name
    return ref


def guarded_print_client(endpoint: str, *, policy: Policy | None = None, **kw: Any):
    """``fakeprint.PrintClient(endpoint, **kw)`` after the endpoint was checked (loopback or a fake uuid in it)."""
    from . import PrintClient

    (policy or current_policy()).assert_allowed(endpoint, "open")
    return PrintClient(endpoint, **kw)
