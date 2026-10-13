"""Shared fixtures: synthetic schema text (ours, no Microsoft content) and the optional real package."""

from __future__ import annotations

from pathlib import Path

import pytest

from fakeprint import services

T = "urn:example:svc"
OTHER = "urn:example:other"

COMMON_XSD = f"""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:t="{T}"
    targetNamespace="{T}" elementFormDefault="qualified">
  <xs:annotation><xs:documentation>  Shared   bits.
  </xs:documentation></xs:annotation>
  <xs:attribute name="Flag" type="xs:boolean" default="false"/>
  <xs:simpleType name="ColorWKV">
    <xs:restriction base="xs:NMTOKEN">
      <xs:enumeration value="Red"><xs:annotation><xs:documentation>The red one</xs:documentation></xs:annotation></xs:enumeration>
      <xs:enumeration value="Green"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="ExtPattern">
    <xs:restriction base="xs:NMTOKEN"><xs:pattern value="x-\\i[\\w\\-]*"/></xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="Color"><xs:union memberTypes="t:ColorWKV t:ExtPattern"/></xs:simpleType>
  <xs:simpleType name="Small"><xs:restriction base="xs:int"><xs:minInclusive value="1"/><xs:maxInclusive value="9"/></xs:restriction></xs:simpleType>
  <xs:simpleType name="Tokens"><xs:list itemType="t:Small"/></xs:simpleType>
  <xs:complexType name="ColorType">
    <xs:simpleContent><xs:extension base="t:Color">
      <xs:attribute ref="t:Flag"/>
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:extension></xs:simpleContent>
  </xs:complexType>
  <xs:attributeGroup name="Ids"><xs:attribute name="Id" type="xs:int" use="required"/></xs:attributeGroup>
  <xs:group name="Pair"><xs:sequence><xs:element name="A" type="xs:string"/><xs:element name="B" type="xs:string" minOccurs="0"/></xs:sequence></xs:group>
</xs:schema>"""

MAIN_XSD = f"""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns="{T}" xmlns:t="{T}"
    targetNamespace="{T}" elementFormDefault="qualified">
  <xs:include schemaLocation="common.xsd"/>
  <xs:import namespace="{OTHER}" schemaLocation="extra/other.xsd"/>
  <xs:import namespace="urn:example:missing" schemaLocation="nowhere.xsd"/>
  <xs:complexType name="BaseType" abstract="true">
    <xs:sequence><xs:element name="Id" type="Small"/></xs:sequence>
    <xs:attribute name="Name" type="xs:string" use="required"/>
  </xs:complexType>
  <xs:complexType name="ItemType">
    <xs:complexContent><xs:extension base="BaseType">
      <xs:sequence>
        <xs:element name="Color" type="ColorType">
          <xs:annotation><xs:documentation>Attribute: Flag</xs:documentation></xs:annotation>
        </xs:element>
        <xs:choice minOccurs="0">
          <xs:element name="Left" type="xs:string"/>
          <xs:element name="Right" type="xs:string"/>
        </xs:choice>
        <xs:element name="Tag" type="xs:string" minOccurs="0" maxOccurs="2"/>
        <xs:any namespace="##other" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:attributeGroup ref="Ids"/>
    </xs:extension></xs:complexContent>
  </xs:complexType>
  <xs:complexType name="ListType">
    <xs:sequence><xs:element name="Item" type="ItemType" minOccurs="0" maxOccurs="unbounded"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="GroupedType"><xs:sequence><xs:group ref="Pair"/><xs:element name="C" type="Tokens"/></xs:sequence></xs:complexType>
  <xs:element name="Head" type="xs:string" abstract="true"/>
  <xs:element name="Member" type="xs:string" substitutionGroup="Head"/>
  <xs:complexType name="HolderType"><xs:sequence><xs:element ref="Head"/></xs:sequence></xs:complexType>
  <xs:simpleType name="FaultCodes">
    <xs:restriction base="xs:QName"><xs:enumeration value="t:Oops"/><xs:enumeration value="t:Broken"/></xs:restriction>
  </xs:simpleType>
  <xs:element name="DoItRequest" type="ListType"/>
  <xs:element name="DoItResponse" type="ListType"/>
  <xs:element name="Grouped" type="GroupedType"/>
  <xs:element name="Holder" type="HolderType"/>
  <xs:element name="Item" type="ItemType"/>
  <xs:element name="OrphanRequest" type="ListType"/>
  <xs:element name="OrphanResponse" type="ListType"/>
  <xs:element name="Inline">
    <xs:complexType><xs:sequence><xs:element name="Leaf" type="xs:int"/></xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>"""

OTHER_XSD = f"""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="{OTHER}">
  <xs:element name="Ext" type="xs:string"/>
</xs:schema>"""

CHAMELEON_XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="Cham"><xs:sequence><xs:element name="X" type="xs:int"/></xs:sequence></xs:complexType>
</xs:schema>"""

WSDL = f"""<definitions xmlns="http://schemas.xmlsoap.org/wsdl/" xmlns:s12="http://schemas.xmlsoap.org/wsdl/soap12/"
    xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:wse="http://schemas.xmlsoap.org/ws/2004/08/eventing"
    xmlns:t="{T}" targetNamespace="{T}" name="Svc">
  <types><xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="{T}"><xs:include schemaLocation="main.xsd"/></xs:schema></types>
  <message name="DoItReq"><part name="body" element="t:DoItRequest"/></message>
  <message name="DoItRes"><part name="body" element="t:DoItResponse"/></message>
  <message name="PingEvt"><part name="body" element="t:Item"/></message>
  <portType name="SvcType" wse:EventSource="true">
    <operation name="DoIt">
      <input message="t:DoItReq" wsa:Action="urn:example:svc/DoIt"/>
      <output message="t:DoItRes" wsa:Action="urn:example:svc/DoItResponse"/>
    </operation>
    <operation name="Ping"><output message="t:PingEvt" wsa:Action="urn:example:svc/Ping"/></operation>
  </portType>
  <binding name="SvcBinding" type="t:SvcType">
    <s12:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
    <operation name="DoIt"><s12:operation soapAction="urn:example:svc/DoIt" soapActionRequired="true"/>
      <input><s12:body use="literal"/></input><output><s12:body use="literal"/></output></operation>
  </binding>
  <service name="SvcService"><port name="P" binding="t:SvcBinding"><s12:address location="http://localhost/x"/></port></service>
</definitions>"""


@pytest.fixture()
def pkg(tmp_path: Path) -> Path:
    d = tmp_path / "pkg"
    (d / "extra").mkdir(parents=True)
    (d / "common.xsd").write_text(COMMON_XSD, encoding="utf-8")
    (d / "main.xsd").write_text(MAIN_XSD, encoding="utf-8")
    (d / "extra" / "other.xsd").write_text(OTHER_XSD, encoding="utf-8")
    (d / "svc.wsdl").write_text(WSDL, encoding="utf-8")
    return d


@pytest.fixture()
def ss(pkg: Path):
    from fakeprint import wsd_schema as w

    return w.load([pkg / "svc.wsdl"])


REAL_DIR = services.PRINT.schema_dir()


@pytest.fixture(scope="session")
def real_ss():
    if not (REAL_DIR / "WDPPrint.xsd").is_file():
        pytest.skip("Microsoft WS-Print package not extracted (run scripts/Get-WsdSpecs.ps1 in fakeprint)")
    from fakeprint import wsd_docs

    return wsd_docs.load_profile_schema(services.PRINT)


# ---- embedded server (needs `maturin develop` and the emulator's profiles directory) -------------


def _native_server_class():
    pytest.importorskip("fakeprint._native", reason="native module not built (maturin develop)")
    from fakeprint import _paths
    from fakeprint.server import Server

    if not (_paths.profiles_dir() / "generic-wsd.json").is_file():
        pytest.skip(f"profiles not found in {_paths.profiles_dir()} (set FAKEPRINT_PROFILES)")
    return Server


@pytest.fixture()
def srv():
    """A fresh embedded server on a loopback ephemeral port."""
    server = _native_server_class()()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def pc(srv):
    """A WS-Print client bound to ``srv``."""
    return srv.printer()


@pytest.fixture()
def server_cls():
    return _native_server_class()
