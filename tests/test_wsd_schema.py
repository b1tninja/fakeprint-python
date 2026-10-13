"""Parser and validator tests on synthetic schemas (offline, no Microsoft text)."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import CHAMELEON_XSD, OTHER, T
from fakeprint import wsd_schema as w

XS = w.XS


def q(local: str) -> w.QName:
    return (T, local)


def test_loading_follows_include_and_import(ss):
    files = {Path(d.source).name for d in ss.docs}
    assert {"main.xsd", "common.xsd", "other.xsd"} <= files
    assert (OTHER, "Ext") in ss.elements
    kinds = {(k, t) for _f, k, t in ss.unresolved}
    assert ("location-not-found", "nowhere.xsd") in kinds


def test_prefix_and_qname_resolution(ss):
    assert ss.qstr(q("ItemType")) == "t:ItemType"
    assert ss.qstr((XS, "int")) == "xs:int"
    assert ss.namespaces()["t"] == T
    # unprefixed type refs in main.xsd resolve through the default namespace
    assert ss.complex_types[q("ItemType")].base == q("BaseType")


def test_chameleon_include_adopts_includer_namespace(tmp_path):
    (tmp_path / "cham.xsd").write_text(CHAMELEON_XSD, encoding="utf-8")
    (tmp_path / "top.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:top"><xs:include schemaLocation="cham.xsd"/></xs:schema>',
        encoding="utf-8",
    )
    s = w.load([tmp_path / "top.xsd"])
    assert ("urn:top", "Cham") in s.complex_types


def test_import_resolved_through_search_dir(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "dep.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:dep"><xs:element name="D" type="xs:int"/></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "a" / "m.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:m"><xs:import namespace="urn:dep" schemaLocation="dep.xsd"/></xs:schema>',
        encoding="utf-8",
    )
    s = w.load([tmp_path / "a" / "m.xsd"], search_dirs=[tmp_path / "b"])
    assert ("urn:dep", "D") in s.elements and not s.unresolved


def test_particles_occurs_and_choice(ss):
    rows = ss.effective_rows(ss.complex_types[q("ItemType")])
    names = [(r.particle.name[1] if r.particle.name else "any", r.min_occurs, r.max_occurs, r.choice is not None) for r in rows]
    assert names[0] == ("Id", 1, 1, False)  # base type first
    assert ("Color", 1, 1, False) in names
    assert ("Left", 0, 1, True) in names and ("Right", 0, 1, True) in names
    assert ("Tag", 0, 2, False) in names
    assert names[-1] == ("any", 0, None, False)


def test_attributes_inherited_group_and_global_ref(ss):
    ct = ss.complex_types[q("ItemType")]
    attrs = {ss.qstr(a.name): a for a, _ in ss.effective_attributes(ct)}
    assert attrs["Name"].required and attrs["Id"].required
    color = ss.complex_types[q("ColorType")]
    flag = [a for a, _ in ss.effective_attributes(color)][0]
    assert flag.is_ref and ss.attribute_default(flag) == "false"
    assert ss.effective_any_attribute(color).namespace == "##other"


def test_simple_types_enum_union_pattern_list(ss):
    vals = ss.enumeration(q("Color"))
    assert [v for v, _d, _w in vals] == ["Red", "Green"]
    assert vals[0][1] == "The red one"
    assert ss.patterns(q("Color")) == ["x-\\i[\\w\\-]*"]
    info = ss.simple_info(q("Small"))
    assert (info.kind, info.facets.min_inclusive, info.facets.max_inclusive) == ("int", "1", "9")
    assert ss.simple_info(q("Tokens")).kind == "list"
    assert ss.simple_info(q("ColorType")).kind == "union"  # simpleContent resolves to its value space


def test_substitution_groups_and_groups(ss):
    assert ss.substitution_members(q("Head")) == [q("Member")]
    rows = ss.effective_rows(ss.complex_types[q("GroupedType")])
    assert [r.particle.name[1] for r in rows] == ["A", "B", "C"]
    assert rows[1].min_occurs == 0


def test_documentation_and_attribute_hints(ss):
    assert any(d.doc == "Shared bits." for d in ss.docs)
    row = [r for r in ss.effective_rows(ss.complex_types[q("ItemType")]) if r.particle.name == q("Color")][0]
    assert row.particle.doc_attributes == ["Flag"]


def test_wsdl_operations_actions_and_events(ss):
    ops = {o.name: o for o in ss.operations()}
    assert ops["DoIt"].kind == "request-response"
    assert ops["DoIt"].input_action == "urn:example:svc/DoIt"
    assert ops["DoIt"].output_action == "urn:example:svc/DoItResponse"
    assert ops["DoIt"].soap_action == "urn:example:svc/DoIt" and ops["DoIt"].soap_action_required is True
    assert ops["Ping"].kind == "notification" and ops["Ping"].event_source
    assert ops["Ping"].soap_action is None  # no binding operation: reported as unknown, not invented
    assert ss.wsdls[0].services["SvcService"].ports[0].address == "http://localhost/x"


def test_orphans_faults_extension_points(ss):
    assert ss.orphan_message_elements() == [q("OrphanRequest"), q("OrphanResponse")]
    ((ft, vals),) = ss.fault_enumerations()
    assert ft == q("FaultCodes") and [c for c, _ in vals] == [q("Oops"), q("Broken")]
    pts = ss.extension_points()
    assert ("t:ItemType", "element", "##other", "strict") in pts
    assert ("t:ColorType", "attribute", "##other", "lax") in pts


def test_usages_reverse_index(ss):
    users = ss.usages()[q("ItemType")]
    assert ("element", "t:ListType/t:Item") in users
    assert ("element-decl", "t:Item") in users


def test_to_dict_is_json_serialisable(ss):
    d = ss.to_dict()
    json.dumps(d)
    assert d["operations"][0]["name"] == "DoIt"
    assert d["complex_types"]["t:ItemType"]["base"] == "t:BaseType"
    assert "t:Color" in d["simple_types"]


ITEM = f'<Item xmlns="{T}" xmlns:t="{T}" Name="n" Id="1"><Id>3</Id><Color t:Flag="true">Red</Color>%s</Item>'


def probs(ss, xml, **kw):
    return [str(p) for p in w.validate(ss, xml, **kw)]


def test_validate_ok_and_extension_elements(ss):
    assert probs(ss, ITEM % "") == []
    assert probs(ss, ITEM % f'<Left>l</Left><Tag>a</Tag><Tag>b</Tag><e:Ext xmlns:e="{OTHER}">v</e:Ext>') == []
    assert probs(ss, ITEM.replace("Red", "x-custom") % "") == []  # union member: extension pattern


def test_validate_catches_problems(ss):
    assert any("missing required attribute" in p for p in probs(ss, f'<Item xmlns="{T}" Id="1"><Id>3</Id><Color>Red</Color></Item>'))
    assert probs(ss, ITEM.replace("Red", "Blue") % "") != []
    assert any("out of range" in p or "<=" in p for p in probs(ss, ITEM.replace("<Id>3", "<Id>30") % ""))
    assert any("unexpected element" in p for p in probs(ss, ITEM % "<Left>1</Left><Right>2</Right>"))
    assert any("unexpected element" in p for p in probs(ss, ITEM % "<Tag>1</Tag><Tag>2</Tag><Tag>3</Tag>"))
    assert any("unexpected attribute" in p for p in probs(ss, ITEM.replace('Name="n"', 'Name="n" Bogus="1"') % ""))
    assert any("unexpected element" in p for p in probs(ss, ITEM % f'<Stray xmlns="{T}"/>'))  # wildcard is ##other
    assert any("missing required" in p for p in probs(ss, f'<Item xmlns="{T}" Name="n" Id="1"><Id>3</Id></Item>'))


def test_validate_order_matters(ss):
    xml = f'<Item xmlns="{T}" Name="n" Id="1"><Color>Red</Color><Id>3</Id></Item>'
    assert any("unexpected element" in p for p in probs(ss, xml))


def test_validate_substitution_group_and_inline_and_list(ss):
    assert probs(ss, f'<Holder xmlns="{T}"><Member>x</Member></Holder>') == []
    assert probs(ss, f'<Inline xmlns="{T}"><Leaf>5</Leaf></Inline>') == []
    assert any("integer" in p for p in probs(ss, f'<Inline xmlns="{T}"><Leaf>five</Leaf></Inline>'))
    assert probs(ss, f'<Grouped xmlns="{T}"><A>x</A><C>1 2 3</C></Grouped>') == []
    assert probs(ss, f'<Grouped xmlns="{T}"><A>x</A><C>1 20</C></Grouped>') != []


def test_validate_namespace_alias_and_unknown_element(ss):
    old = "urn:example:svc:old"
    xml = ITEM.replace(T, old) % ""
    assert probs(ss, xml) != []
    assert probs(ss, xml, ns_alias={old: T}) == []
    assert probs(ss, '<Nope xmlns="urn:nope"/>')[0].endswith("no global element declaration")
