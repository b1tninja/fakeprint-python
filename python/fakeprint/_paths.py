"""Where the sibling checkouts live (the Microsoft package is only ever read from a sibling repo)."""

from __future__ import annotations

import os
from pathlib import Path

_THIS_REPO = Path(__file__).resolve().parents[2]


def sibling(name: str, env: str) -> Path:
    v = os.environ.get(env)
    return Path(v) if v else _THIS_REPO.parent / name


def fakeprint_repo() -> Path:
    return sibling("fakeprint", "FAKEPRINT_REPO")


def fakescan_repo() -> Path:
    return sibling("fakescan", "FAKESCAN_REPO")


def profiles_dir() -> Path:
    """Capability profiles the emulator ships (``FAKEPRINT_PROFILES`` overrides)."""
    v = os.environ.get("FAKEPRINT_PROFILES")
    return Path(v) if v else fakeprint_repo() / "profiles"


def list_profiles(directory: Path | str | None = None) -> list[str]:
    """Ids of the ``<id>.json`` profiles in ``directory`` (default: the emulator profiles dir)."""
    d = Path(directory) if directory is not None else profiles_dir()
    return sorted(p.stem for p in d.glob("*.json"))
