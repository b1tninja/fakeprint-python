"""Offline unit tests for the SOAP client, fault parser and PrintTicket (no server, no native)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from fakeprint import soap
from fakeprint.soap import (
    HttpError,
    JobIdNotFound,
    PrintClient,
    SoapFault,
    parse_fault,
)
from fakeprint.ticket import PRINT_NS, PrintTicket

FAULT = """<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:wprt="http://schemas.microsoft.com/windows/2006/08/wdp/print">
  <soap:Header><wsa:Action>http://schemas.xmlsoap.org/ws/2004/08/addressing/fault</wsa:Action></soap:Header>
  <soap:Body><soap:Fault>
    <soap:Code><soap:Value>soap:Sender</soap:Value>
      <soap:Subcode><soap:Value>wprt:ClientErrorJobIdNotFound</soap:Value></soap:Subcode></soap:Code>
    <soap:Reason><soap:Text xml:lang="en">no such job</soap:Text></soap:Reason>
    <soap:Detail><wprt:JobId>42</wprt:JobId></soap:Detail>
  </soap:Fault></soap:Body>
</soap:Envelope>"""

PLAIN_FAULT = FAULT.replace(
    "<soap:Subcode><soap:Value>wprt:ClientErrorJobIdNotFound</soap:Value></soap:Subcode>", ""
).replace("soap:Sender", "soap:Receiver")


def test_fault_with_subcode():
    f = parse_fault(500, FAULT)
    assert isinstance(f, JobIdNotFound) and isinstance(f, SoapFault)
    assert f.is_sender and not f.is_receiver
    assert f.subcode == "wprt:ClientErrorJobIdNotFound"
    assert f.subcode_local == "ClientErrorJobIdNotFound"
    assert f.subcode_ns == PRINT_NS
    assert f.reason == "no such job"
    assert f.detail and "42" in f.detail
    assert f.subcode_local in soap.KNOWN_SUBCODES


def test_fault_without_subcode():
    f = parse_fault(500, PLAIN_FAULT)
    assert f is not None and f.subcode is None and f.is_receiver and type(f) is SoapFault


def test_nested_subcode_deepest_wins():
    nested = FAULT.replace(
        "<soap:Subcode><soap:Value>wprt:ClientErrorJobIdNotFound</soap:Value></soap:Subcode>",
        "<soap:Subcode><soap:Value>wprt:OperationFailed</soap:Value>"
        "<soap:Subcode><soap:Value>wprt:ServerErrorDocumentActive</soap:Value></soap:Subcode></soap:Subcode>",
    )
    assert parse_fault(500, nested).subcode_local == "ServerErrorDocumentActive"


@pytest.mark.parametrize("text", ["", "not xml", "<a/>", "<soap:Envelope xmlns:soap='x'/>"])
def test_non_fault_bodies(text):
    assert parse_fault(500, text) is None


def test_envelope_headers():
    env = PrintClient.envelope("GetActiveJobs", "<wprt:GetActiveJobsRequest/>", to="http://127.0.0.1:1/x")
    root = ET.fromstring(env)
    hdr = root.find(f"{{{soap.SOAP_NS}}}Header")
    assert hdr.find(f"{{{soap.WSA_NS}}}Action").text == f"{PRINT_NS}/GetActiveJobs"
    assert hdr.find(f"{{{soap.WSA_NS}}}MessageID").text.startswith("urn:uuid:")
    assert hdr.find(f"{{{soap.WSA_NS}}}To").text == "http://127.0.0.1:1/x"


def test_http_error_type():
    e = HttpError(503, "busy")
    assert e.status_code == 503 and "503" in str(e)


# ---- PrintTicket --------------------------------------------------------------------------


def test_ticket_minimal_is_description_only():
    xml = PrintTicket(job_name="a&b", user="u").to_xml()
    root = ET.fromstring(f'<r xmlns:wprt="{PRINT_NS}">{xml}</r>')
    t = root[0]
    assert [soap.local(c.tag) for c in t] == ["JobDescription"]
    assert t.find(soap.q("JobDescription")).find(soap.q("JobName")).text == "a&b"


def test_ticket_full_order_and_must_honor():
    t = PrintTicket(
        copies=2,
        collate=True,
        priority=50,
        media_size="iso_a4_210x297mm",
        media_type="stationery",
        media_color="white",
        pages_per_sheet=2,
        orientation="Landscape",
        resolution=600,
        print_quality="High",
        sides="TwoSidedLongEdge",
        must_honor=frozenset({"copies", "sides"}),
    )
    root = ET.fromstring(f'<r xmlns:wprt="{PRINT_NS}">{t.to_xml()}</r>')[0]
    assert [soap.local(c.tag) for c in root] == ["JobDescription", "JobProcessing", "DocumentProcessing"]
    doc = root.find(soap.q("DocumentProcessing"))
    assert [soap.local(c.tag) for c in doc] == [
        "MediaSizeName",
        "MediaType",
        "MediaColor",
        "NumberUp",
        "Orientation",
        "Resolution",
        "PrintQuality",
        "Sides",
    ]
    mh = f"{{{PRINT_NS}}}MustHonor"
    assert doc.find(soap.q("Sides")).get(mh) == "true"
    assert doc.find(soap.q("Orientation")).get(mh) is None
    assert root.find(soap.q("JobProcessing")).find(soap.q("Copies")).get(mh) == "true"
    # and it reads back
    back = PrintTicket.from_element(root)
    assert back == t.__class__(**{**t.__dict__, "resolution": (600, 600), "nup_direction": "RightDown"})


@pytest.mark.parametrize(
    "kw",
    [{"copies": 0}, {"priority": 0}, {"priority": 101}, {"job_name": "x" * 256}, {"must_honor": frozenset({"nope"})}],
)
def test_ticket_rejects_out_of_range(kw):
    with pytest.raises(ValueError):
        PrintTicket(**kw).to_xml()


def test_ticket_validates_against_schema(real_ss):
    from fakeprint import wsd_schema as w

    t = PrintTicket(copies=1, collate=False, priority=10, media_size="iso_a4_210x297mm", pages_per_sheet=4, resolution=(300, 300), sides="OneSided")
    body = f'<wprt:CreatePrintJobRequest xmlns:wprt="{PRINT_NS}">{t.to_xml()}</wprt:CreatePrintJobRequest>'
    assert w.validate(real_ss, body) == []


def test_send_document_requests_validate_against_schema(real_ss):
    from fakeprint import wsd_schema as w

    body = (
        f'<wprt:SendDocumentRequest xmlns:wprt="{PRINT_NS}"><wprt:JobId>1</wprt:JobId>'
        "<wprt:DocumentDescription><wprt:DocumentId>1</wprt:DocumentId><wprt:Compression>None</wprt:Compression>"
        "<wprt:Format>application/pdf</wprt:Format></wprt:DocumentDescription>"
        "<wprt:LastDocument>true</wprt:LastDocument><wprt:DocumentData>AAAA</wprt:DocumentData></wprt:SendDocumentRequest>"
    )
    assert w.validate(real_ss, body) == []
