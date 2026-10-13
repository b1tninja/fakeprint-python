"""registry.py and ui.py: parsing fixtures, the 3.4 matching rules, capability classes, tray semantics."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from _multi import device_info, uuid_for

from fakeprint import registry, ui
from fakeprint.registry import (
    ManagedDevice,
    WindowsPrinter,
    capability_class,
    match_kind,
    match_printers_to_devices,
    port_uuids,
    reconcile,
)

U1, U2 = uuid_for(0xA1), uuid_for(0xB1)


def dev(id="mono", n=0xA1, **kw):
    return ManagedDevice.parse(device_info(id, n, **kw))


# ---- parsing ----------------------------------------------------------------------------------


def test_managed_device_parses_the_section_2_2_row():
    d = dev(install={"state": "installed", "kind": "wsd", "windows": {"name": "fakeprint mono", "port": f"WSD-{U1}"}})
    assert (d.id, d.uuid, d.ipp_uuid, d.profile) == ("mono", U1, uuid_for(0xA2), "mono-laser")
    assert d.state == "installed" and d.installed and d.install.windows.name == "fakeprint mono"
    assert d.klass == "3fa9c2d1e07b" == d.config.klass
    assert d.config.summary.media_sizes == ["iso_a4_210x297mm"] and d.config.summary.versions["v20"] is False
    assert d.live["faults"]["offline"] is False and d.service_endpoint.endswith("/d/mono/WebServices/PrinterService")


def test_parse_is_lenient_and_keeps_unknown_fields():
    d = ManagedDevice.parse({"id": "x", "uuid": U1, "profile": "p", "future_field": 7, "install": {"state": "probed", "novel": 1}})
    assert d.extra == {"future_field": 7} and d.install.extra == {"novel": 1}
    assert d.config is None and d.klass == "" and d.queue_position is None


def test_registry_document_and_legacy_single_row():
    reg = registry.parse({
        "advertise_default": "", "port_policy": {"base": 3921, "step": 2}, "queue_order": ["b", "a"], "other": 1,
        "devices": [
            {"name": "a", "listen": "127.0.0.1:3921", "device_uuid": U1, "profile": "mono-laser", "discovery": False,
             "install": {"state": "stale", "reason": "profile changed: formats"}},
            {"name": "legacy", "listen": "0.0.0.0:3921", "device_uuid": U2, "added": {"pnp": True}},
        ],
    })
    assert [d.name for d in reg.devices] == ["a", "legacy"] and reg.queue_order == ["b", "a"] and reg.extra == {"other": 1}
    a, legacy = reg.find("a"), reg.find("legacy")
    assert a.needs_readd and a.needs_readd_reason == "profile changed: formats" and a.install.profile_changed
    assert legacy.install.state == "installed" and legacy.install.note == registry.LEGACY_NOTE and legacy.added_pnp
    assert [d.name for d in reg.rows_for_listen("127.0.0.1:3921")] == ["a"]


def test_load_missing_file_is_empty_and_never_writes(tmp_path):
    p = tmp_path / "devices.json"
    assert registry.load(p).devices == []
    assert not p.exists() and not (tmp_path / "devices.json.lock").exists()
    p.write_text("﻿" + json.dumps({"devices": [{"name": "m", "device_uuid": U1}]}), encoding="utf-8")
    assert registry.load(p).find("m").uuid == U1


def test_load_retries_a_torn_file_then_reads(tmp_path, monkeypatch):
    p = tmp_path / "devices.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(time, "sleep", lambda s: p.write_text('{"devices": []}', encoding="utf-8"))
    assert registry.load(p, retries=3).devices == []
    p.write_text("{still bad", encoding="utf-8")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(OSError):
        registry.load(p, retries=2)


def test_registry_path_honours_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKEPRINT_REGISTRY", str(tmp_path / "r.json"))
    assert registry.registry_path() == tmp_path / "r.json"
    monkeypatch.delenv("FAKEPRINT_REGISTRY")
    assert registry.registry_path().name == "devices.json" and "fakeprint" in str(registry.registry_path())


# ---- matching (docs/multi-device.md 3.4) -------------------------------------------------------


def test_port_uuids_normalises():
    assert port_uuids(f"WSD-{U1.upper()}") == {U1}
    assert port_uuids(f"{{{U1}}} urn:uuid:{U2}") == {U1, U2}
    assert port_uuids("192.168.1.5:9100") == set()


@pytest.mark.parametrize(
    "port,expected",
    [
        (f"WSD-{U1}", "wsd"),
        (f"urn:uuid:{U1}", "wsd"),
        ("http://127.0.0.1:3921/d/mono/ipp/print", "ipp"),
        ("ipp://127.0.0.1:3921/d/mono/ipp/print", "ipp"),
        ("http://127.0.0.1:3921/ipp/print", None),          # another device's path (the legacy one)
        ("http://127.0.0.1:9999/d/mono/ipp/print", None),   # another authority
        ("127.0.0.1:9100", None),                           # TCPMON
        (f"WSD-{U1} WSD-{U2}", None),                       # two UUIDs: ambiguous
        (f"WSD-{U2}", None),                                # a UUID that is not this device's
        (f"WSD-{uuid_for(0xA2)}", None),                    # the IPP UUID is not the WSD identity
        ("WSD-fakeprint mono", None),                       # looks similar, proves nothing
        ("", None),
    ],
)
def test_match_kind(port, expected):
    assert match_kind(dev(), WindowsPrinter(name="fakeprint mono", port=port)) == expected


def test_name_alone_never_matches():
    m = match_printers_to_devices([dev()], [{"name": "fakeprint mono", "port": "LPT1:"}])
    assert not m.matched and [p.name for p in m.foreign] == ["fakeprint mono"]


def test_match_printers_to_devices_classifies_everything():
    devs = [dev("mono", 0xA1), dev("color", 0xB1)]
    listing = [
        {"name": "A", "port": f"WSD-{U1}"},
        {"name": "A again", "port": f"WSD-{U1}"},
        {"name": "Real MFP", "port": "192.168.1.9:9100"},
        {"name": "C", "port": "http://127.0.0.1:3921/d/color/ipp/print"},
    ]
    m = match_printers_to_devices(devs, listing)
    assert set(m.matched) == {"mono", "color"} and m.kinds == {"mono": "wsd", "color": "ipp"}
    assert [p.name for p in m.duplicates] == ["A again"] and [p.name for p in m.foreign] == ["Real MFP"]


def test_matching_accepts_attribute_objects_and_rows():
    class P:  # shaped like winprint.PrinterInfo
        name, port, driver, status = "fakeprint mono", f"WSD-{U1}", "drv", 0

    m = match_printers_to_devices([registry.DeviceRow(name="mono", device_uuid=U1)], [P()])
    assert m.matched["mono"].driver == "drv"


def test_reconcile_marks_missing_installed_devices_offline_not_an_add_request():
    installed = dev("mono", 0xA1, install={"state": "installed"})
    other = dev("color", 0xB1, install={"state": "installed"})
    fresh = dev("new", 0xC1)  # never installed: not stale just because it is unlisted
    r = reconcile([installed, other, fresh], [{"name": "x", "port": f"WSD-{U1}"}, {"name": "hw", "port": "10.0.0.2:9100"}])
    assert r.installed == ["mono"] and r.stale == ["color"] == r.offline and [p.name for p in r.foreign] == ["hw"]


# ---- capability class --------------------------------------------------------------------------


def test_capability_class_and_selection():
    assert capability_class("fnv64:3fa9c2d1e07b4455") == "3fa9c2d1e07b"
    assert capability_class("") == "" and capability_class("fnv64:xyz") == "" and capability_class("fnv64:abc") == ""
    a = dev("a", 0xA1, install={"state": "installed"})
    b = dev("b", 0xB1)  # same class, not installed
    c = dev("c", 0xC1, install={"state": "installed"}, config={"published_fingerprint": "fnv64:ffffffffffffffff"})
    assert registry.classes([a, b, c]) == {"3fa9c2d1e07b": ["a", "b"], "ffffffffffff": ["c"]}
    assert [d.id for d in registry.select_installed([a, b, c], "3fa9c2d1e07b")] == ["a"]
    assert [d.id for d in registry.select_installed([a, b, c], profile="mono-laser")] == ["a", "c"]


def test_skip_message_names_the_candidates_and_what_a_human_does():
    b = dev("b", 0xB1)
    msg = registry.skip_message([b], "3fa9c2d1e07b")
    assert "fakeprint b (new)" in msg and "Add device" in msg and "nothing is added automatically" in msg
    assert "no fakeprint printer of class zzz is registered" in registry.skip_message([b], "zzz")


# ---- ui semantics ------------------------------------------------------------------------------


def row(**kw):
    inst = kw.pop("install", {"state": "installed"})
    live = kw.pop("live", None)
    info = device_info(install=inst, **kw)
    if live:
        info["live"]["faults"].update(live)
    return ui.StatusRow.from_device(ManagedDevice.parse(info))


def test_guidance_text_is_the_contract_constant():
    g = ui.add_guidance("fakeprint mono", 2, 5)
    assert g == (
        'Add now: fakeprint mono (2 of 5). Open Settings > Bluetooth & devices > Printers & scanners > Add device, '
        'wait for "fakeprint mono" to appear and choose Add. Windows takes about 30 seconds; fakeprint announces the '
        "next printer only after this one is installed."
    )


def test_summary_level_cases():
    assert ui.summary_level(None) == "red"
    assert ui.summary_level([]) == "green"
    assert ui.summary_level([row(), row(id="b")]) == "green"
    assert ui.summary_level([row(), row(id="n", install={"state": "new"})]) == "amber"
    assert ui.summary_level([row(install={"state": "probed"})]) == "amber"
    assert ui.summary_level([row(install={"state": "stale", "reason": "profile changed: media"}, needs_readd=True)]) == "amber"
    # offline (not listed by Windows) is not an Add request
    off = row(install={"state": "stale", "reason": "not listed by Windows"}, needs_readd=True, needs_readd_reason="not listed by Windows")
    assert off.offline and ui.summary_level([off]) == "green" and off.status_text() == "offline"
    # a raised fault is amber, whatever the install state
    assert ui.summary_level([row(live={"paper_out": True})]) == "amber"
    assert row(live={"paper_out": True, "offline": True}).faults == ["paper_out", "offline"]


def test_profile_changed_stale_is_held_for_the_grace_period():
    now = datetime.now(timezone.utc)

    def stale(age_s):
        return row(
            install={"state": "stale", "reason": "profile changed: formats", "stale_at": (now - timedelta(seconds=age_s)).isoformat()},
            needs_readd=True, needs_readd_reason="profile changed: formats",
        )

    t = now.timestamp()
    fresh, old = stale(5), stale(60)
    assert fresh.settling(t) and not fresh.wants_add(t) and fresh.status_text(t) == "profile changed, confirming"
    assert ui.summary_level([fresh], t) == "green"
    assert not old.settling(t) and old.wants_add(t) and ui.summary_level([old], t) == "amber"
    assert old.status_text(t) == "needs re-Add (profile changed: formats)"


def test_status_text_variants():
    assert row().status_text() == "installed"
    assert row(install={"state": "new"}, announcing=True, queue_position=1).status_text() == "needs Add"
    assert row(install={"state": "new"}, announcing=False, queue_position=3).status_text() == "waiting in queue"


def test_summary_text_and_table():
    rows = [row(), row(id="n", install={"state": "new"}, announcing=True, queue_position=1),
            row(id="o", install={"state": "stale", "reason": "not listed by Windows"})]
    assert ui.summary_text(rows) == "fakeprint: 3 printers, 1 installed, 1 need Add, 1 offline"
    assert ui.summary_text(None) == "fakeprint: daemon not reachable" and len(ui.summary_text(rows)) <= 120
    table = ui.format_status_table(rows, 200)
    lines = table.splitlines()
    assert lines[0].split()[:3] == ["ID", "NAME", "STATE"] and "new!" in table and "mono" in lines[2]
    assert ui.format_status_table([], 80) == "(no printers)"
    narrow = ui.format_status_table(rows, 50)
    assert max(len(l) for l in narrow.splitlines()) <= 50 and "UUID" not in narrow.splitlines()[0]


def test_profile_info_parses_summary_fields():
    p = ui.ProfileInfo.parse({"id": "m", "label": "Mono", "formats": ["a"], "color_modes": ["Monochrome"], "versions": {"v20": True}})
    assert (p.id, p.description, p.color_modes, p.versions) == ("m", "Mono", ["Monochrome"], {"v20": True})
