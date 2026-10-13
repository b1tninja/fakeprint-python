"""Doc generator tests: synthetic package (offline) and, when present, the real extracted package."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from fakeprint import services, wsd_docs
from fakeprint import wsd_schema as w
from conftest import REAL_DIR

PROFILE = services.ServiceProfile(key="x", title="Example", repo="fakeprint", concepts=("Item", "Color"),
                                  ticket_markers=("Item",), probe_operations=("DoIt", "Missing"))


def _links_ok(root: Path) -> list[str]:
    bad = []
    for md in root.rglob("*.md"):
        for target in re.findall(r"\]\(([^)#\s]+\.md)\)", md.read_text(encoding="utf-8")):
            if not (md.parent / target).is_file():
                bad.append(f"{md.relative_to(root)} -> {target}")
    return bad


@pytest.fixture()
def built(ss, tmp_path):
    out = tmp_path / "out"
    wsd_docs.build(ss, PROFILE, out)
    return out


def test_pages_exist_and_links_resolve(built):
    for name in ("index.md", "faults.md", "enumerations.md", "attributes.md", "extension-points.md",
                 "package.md", "ticket.md", "elements.md", "schema-model.json", "operations/index.md",
                 "operations/DoIt.md", "operations/Ping.md", "operations/Orphan.md", "types/ItemType.md",
                 "concepts/Item.md", "concepts/Color.md"):
        assert (built / name).is_file(), name
    assert _links_ok(built) == []


def test_operation_page_has_actions_and_shape(built):
    text = (built / "operations" / "DoIt.md").read_text(encoding="utf-8")
    assert "urn:example:svc/DoIt" in text and "urn:example:svc/DoItResponse" in text
    assert "`t:Item`" in text and "[0..*]" in text
    idx = (built / "operations" / "index.md").read_text(encoding="utf-8")
    assert "| DoIt | yes |" in idx and "| Missing | no |" in idx
    assert "Orphan" in idx  # message-only elements surfaced, action marked unknown
    assert "unknown" in (built / "operations" / "Orphan.md").read_text(encoding="utf-8")


def test_type_page_enumeration_and_attributes(built):
    text = (built / "types" / "ColorWKV.md").read_text(encoding="utf-8")
    assert "`Red`" in text and "The red one" in text
    item = (built / "types" / "ItemType.md").read_text(encoding="utf-8")
    assert "| Name |" in item and "choice" in item


def test_json_model_and_stale_cleanup(ss, tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    (out / "99-Stale.md").write_text("x")
    wsd_docs.build(ss, PROFILE, out)
    assert not (out / "99-Stale.md").exists()
    model = json.loads((out / "schema-model.json").read_text(encoding="utf-8"))
    assert {o["name"] for o in model["operations"]} == {"DoIt", "Ping"}


def test_main_with_explicit_dirs(pkg, tmp_path, capsys):
    out = tmp_path / "cli"
    assert wsd_docs.main(["--schema-dir", str(pkg), "--out-dir", str(out)]) == 0
    assert (out / "index.md").is_file()


def test_missing_schema_dir_message(tmp_path):
    with pytest.raises(FileNotFoundError):
        wsd_docs.generate_reference_docs("print", tmp_path / "nope")


# ---- real package (skipped when the Microsoft files are not extracted) ---------------------

pytestmark_real = pytest.mark.package


@pytest.mark.package
def test_real_operation_set(real_ss):
    names = {o.name: o for o in real_ss.operations()}
    assert set(names) >= {"CreatePrintJob", "SendDocument", "AddDocument", "CancelJob", "GetPrinterElements",
                          "GetJobElements", "GetActiveJobs", "GetJobHistory", "SetEventRate"}
    events = {n for n, o in names.items() if o.kind == "notification"}
    assert events == {"PrinterElementsChangeEvent", "PrinterStatusSummaryEvent", "PrinterStatusConditionEvent",
                      "PrinterStatusConditionClearedEvent", "JobStatusEvent", "JobEndStateEvent"}
    assert names["CreatePrintJob"].soap_action.endswith("/wdp/print/CreatePrintJob")
    # no ticket validation / administrative operations exist in any shipped version
    for absent in ("ValidatePrintTicket", "PausePrinter", "ResumePrinter", "PurgeJobs"):
        assert absent not in names and not any(q[1].startswith(absent) for q in real_ss.elements)


@pytest.mark.package
def test_real_core_facts(real_ss):
    ss = real_ss
    p = "http://schemas.microsoft.com/windows/2006/08/wdp/print"
    resp = ss.complex_types[(p, "CreatePrintJobResponseType")]
    assert [r.particle.name[1] for r in ss.effective_rows(resp)] == ["JobId"]  # no JobToken in the schema
    (ft, vals), = ss.fault_enumerations()
    assert ft == (p, "PrintServiceFaultValuesType") and len(vals) == 15
    assert (p, "ClientErrorFormatNotSupported") in [c for c, _ in vals]
    assert (p, "ClientErrorConflictingRequiredParameters") not in [c for c, _ in vals]
    states = [v for v, _d, _w in ss.enumeration((p, "JobStateType"))]
    assert states == ["Aborted", "Canceled", "Completed", "Pending", "Pending-Held", "Processing",
                      "ProcessingStopped", "Started", "Terminating"]
    mh = ss.attributes[(p, "MustHonor")]
    assert mh.default == "false"
    assert (p, "PrintTicketType") in ss.complex_types
    assert ss.unresolved == [] and ss.duplicates == []


@pytest.mark.package
def test_real_extension_namespaces_and_message_only_ops(real_ss):
    ns = {n for n in real_ss.target_namespaces()}
    assert any(n.endswith("/printV12") for n in ns) and any(n.endswith("/printV20") for n in ns)
    orphans = {q[1] for q in real_ss.orphan_message_elements()}
    assert {"CreatePrintJob2Request", "GetPrintDeviceCapabilitiesRequest", "PrepareToPrintRequest"} <= orphans


@pytest.mark.package
def test_real_validate_sample_instances(real_ss):
    p = "http://schemas.microsoft.com/windows/2006/08/wdp/print"
    good = (f'<wprt:CreatePrintJobRequest xmlns:wprt="{p}"><wprt:PrintTicket>'
            "<wprt:JobDescription><wprt:JobName>t</wprt:JobName><wprt:JobOriginatingUserName>u</wprt:JobOriginatingUserName></wprt:JobDescription>"
            f'<wprt:DocumentProcessing><wprt:Orientation wprt:MustHonor="true">Portrait</wprt:Orientation></wprt:DocumentProcessing>'
            "</wprt:PrintTicket></wprt:CreatePrintJobRequest>")
    assert [str(x) for x in w.validate(real_ss, good)] == []
    bad = good.replace("Portrait", "Sideways")
    assert w.validate(real_ss, bad)
    # the response carries only JobId: a JobToken sibling is not in the schema (the base schema has no wildcard here)
    resp = f'<wprt:CreatePrintJobResponse xmlns:wprt="{p}"><wprt:JobId>5</wprt:JobId><wprt:JobToken>x</wprt:JobToken></wprt:CreatePrintJobResponse>'
    assert any("JobToken" in str(x) for x in w.validate(real_ss, resp))
    assert not w.validate(real_ss, f'<wprt:CreatePrintJobResponse xmlns:wprt="{p}"><wprt:JobId>5</wprt:JobId></wprt:CreatePrintJobResponse>')


@pytest.mark.package
def test_real_generation(real_ss, tmp_path):
    out = tmp_path / "gen"
    written = wsd_docs.build(real_ss, services.PRINT, out)
    assert len(written) > 250
    assert _links_ok(out) == []
    model = json.loads((out / "schema-model.json").read_text(encoding="utf-8"))
    assert len(model["operations"]) == 16
    for must in ("operations/CreatePrintJob.md", "operations/SendDocument.md", "operations/JobEndStateEvent.md",
                 "concepts/JobState.md", "concepts/Resolution.md", "concepts/PrinterCapabilities.md", "faults.md"):
        assert (out / must).is_file(), must
    assert "Pending-Held" in (out / "concepts" / "JobState.md").read_text(encoding="utf-8")


@pytest.mark.package
def test_same_core_parses_scan_package(tmp_path):
    scan = services.SCAN.schema_dir()
    if not (scan / "WDPScan.xsd").is_file():
        pytest.skip("scan package not extracted")
    ss = wsd_docs.load_profile_schema(services.SCAN)
    assert {o.name for o in ss.operations()} >= {"CreateScanJob", "RetrieveImage", "ValidateScanTicket"}
    wsd_docs.build(ss, services.SCAN, tmp_path / "scan")
    assert (tmp_path / "scan" / "operations" / "CreateScanJob.md").is_file()
