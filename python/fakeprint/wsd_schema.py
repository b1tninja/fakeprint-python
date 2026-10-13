"""Service-agnostic XSD + WSDL model for the Microsoft WSD service definitions.

One implementation serves every WSD service package (Print, Scan, ...): nothing in here names a
service, an element or a namespace of one. Service specific knowledge (page layout, fault type
hints, namespace aliases) is *data* in :mod:`fakeprint.services`.

What is modelled (stdlib only, ``xml.etree`` plus a small scope tracker):

* namespaces and prefixes per document, QName resolution (``{ns}local`` tuples)
* ``xs:include`` / ``xs:import`` across a multi-file package, including imports that point to
  a sibling directory, chameleon includes (no targetNamespace) and unresolved locations
* global ``element`` / ``complexType`` / ``simpleType`` / ``attribute`` / ``attributeGroup`` /
  ``group`` declarations, with documentation (``xs:annotation``)
* complex types: sequence / choice / all / group refs, ``minOccurs`` / ``maxOccurs``,
  extension and restriction (simple and complex content), attributes (``use``, ``default``,
  ``fixed``, global ``ref`` attributes such as ``MustHonor``), ``xs:any`` and ``xs:anyAttribute``
  (the vendor extension points), ``abstract`` / ``mixed``
* simple types: restriction facets (enumeration with documentation, pattern, lengths, ranges),
  unions, lists
* substitution groups
* WSDL 1.1: messages, portTypes (including the ``wse:EventSource`` flag), operations with
  input/output/fault messages and ``wsa:Action`` URIs, SOAP 1.2 bindings (``soapAction``),
  services/ports, policies
* instance validation of an XML element against the model (:func:`validate`)

The model is intentionally close to the XSD so that generators (docs, Rust code, validators)
can all walk the same graph; :meth:`SchemaSet.to_dict` dumps it as JSON-able data.
"""

from __future__ import annotations

import io
import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator
from xml.etree import ElementTree as ET

__all__ = [
    "AnyAttr",
    "AttributeUse",
    "Binding",
    "ComplexType",
    "Element",
    "Enum",
    "Facets",
    "Operation",
    "OperationView",
    "Particle",
    "PortType",
    "Problem",
    "QName",
    "SchemaDoc",
    "SchemaSet",
    "SimpleType",
    "Wsdl",
    "XS",
    "load",
    "validate",
]

XS = "http://www.w3.org/2001/XMLSchema"
XML_NS = "http://www.w3.org/XML/1998/namespace"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
WSDL = "http://schemas.xmlsoap.org/wsdl/"
WSDL_SOAP12 = "http://schemas.xmlsoap.org/wsdl/soap12/"
WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
WSAM = "http://www.w3.org/2007/05/addressing/metadata"
WSE = "http://schemas.xmlsoap.org/ws/2004/08/eventing"
WSP = "http://schemas.xmlsoap.org/ws/2004/09/policy"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"

QName = tuple[str, str]  # (namespace uri or "", local name)

#: Built-in XSD types the validator and doc generator know how to talk about.
_BUILTIN_KINDS: dict[str, str] = {
    "string": "string", "normalizedString": "string", "token": "string", "language": "string",
    "Name": "string", "NCName": "string", "ID": "string", "IDREF": "string",
    "NMTOKEN": "nmtoken", "NMTOKENS": "nmtokens",
    "boolean": "boolean",
    "int": "int", "integer": "int", "long": "int", "short": "int", "byte": "int",
    "nonNegativeInteger": "int", "positiveInteger": "int", "nonPositiveInteger": "int",
    "negativeInteger": "int", "unsignedInt": "int", "unsignedLong": "int",
    "unsignedShort": "int", "unsignedByte": "int",
    "decimal": "number", "double": "number", "float": "number",
    "dateTime": "dateTime", "date": "date", "time": "time", "duration": "string",
    "anyURI": "anyURI", "QName": "QName", "base64Binary": "base64", "hexBinary": "hex",
    "anySimpleType": "string",
}
_INT_RANGES: dict[str, tuple[int | None, int | None]] = {
    "int": (-(2**31), 2**31 - 1), "short": (-(2**15), 2**15 - 1), "byte": (-128, 127),
    "long": (-(2**63), 2**63 - 1), "nonNegativeInteger": (0, None), "positiveInteger": (1, None),
    "nonPositiveInteger": (None, 0), "negativeInteger": (None, -1), "unsignedInt": (0, 2**32 - 1),
    "unsignedShort": (0, 65535), "unsignedByte": (0, 255), "unsignedLong": (0, 2**64 - 1),
}


# --------------------------------------------------------------------------------------------
# generic XML tree with namespace scope
# --------------------------------------------------------------------------------------------


class Node:
    """XML element with its in-scope namespace declarations (what ElementTree drops)."""

    __slots__ = ("ns", "local", "attrib", "scope", "children", "text", "alltext", "source")

    def __init__(self, el: ET.Element, scope: dict[str, str], source: str) -> None:
        self.ns, self.local = _split(el.tag)
        self.attrib: dict[QName, str] = {_split(k): v for k, v in el.attrib.items()}
        self.scope = scope
        self.children: list[Node] = []
        self.text = el.text or ""
        self.alltext = ""
        self.source = source

    def get(self, name: str, ns: str = "") -> str | None:
        return self.attrib.get((ns, name))

    def kids(self, local: str, ns: str | None = XS) -> Iterator["Node"]:
        for c in self.children:
            if c.local == local and (ns is None or c.ns == ns):
                yield c

    def kid(self, local: str, ns: str | None = XS) -> "Node | None":
        return next(self.kids(local, ns), None)

    def qname(self, value: str, *, default_ns: bool = True) -> QName:
        """Resolve a prefixed QName attribute value in this element's scope."""
        value = value.strip()
        if ":" in value:
            prefix, local = value.split(":", 1)
            return (self.scope.get(prefix, ""), local)
        return (self.scope.get("", "") if default_ns else "", value)


def _split(tag: str) -> QName:
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return "", tag


def parse_xml(data: str | bytes, source: str = "<string>") -> Node:
    if isinstance(data, str):
        data = data.encode("utf-8")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    scopes: list[dict[str, str]] = [{"xml": XML_NS}]
    pending: list[tuple[str, str]] = []
    stack: list[Node] = []
    root: Node | None = None
    for event, obj in ET.iterparse(io.BytesIO(data), events=("start-ns", "start", "end")):
        if event == "start-ns":
            pending.append(obj)
        elif event == "start":
            scope = dict(scopes[-1])
            scope.update(pending)
            pending = []
            scopes.append(scope)
            node = Node(obj, scope, source)
            if stack:
                stack[-1].children.append(node)
            else:
                root = node
            stack.append(node)
        else:
            node = stack.pop()
            scopes.pop()
            if node.ns in (XS, WSDL) and node.local in ("documentation", "appinfo"):
                node.alltext = "".join(obj.itertext())
    assert root is not None
    return root


def _clean_doc(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.strip().splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def _docs(node: Node) -> str:
    parts = []
    for ann in node.kids("annotation"):
        for d in ann.kids("documentation"):
            t = _clean_doc(d.alltext)
            if t:
                parts.append(t)
    return "\n\n".join(parts)


def _occurs(node: Node) -> tuple[int, int | None]:
    lo = node.get("minOccurs")
    hi = node.get("maxOccurs")
    try:
        mn = int(lo) if lo is not None else 1
    except ValueError:
        mn = 1
    if hi is None:
        mx: int | None = 1
    elif hi == "unbounded":
        mx = None
    else:
        try:
            mx = int(hi)
        except ValueError:
            mx = 1
    return mn, mx


_ATTR_HINT = re.compile(r"^\s*Attributes?\s*:\s*(.+?)\s*$", re.I | re.M)


def _doc_attribute_hints(doc: str) -> list[str]:
    """``Attribute: MustHonor`` style hints some schemas put in element documentation."""
    out: list[str] = []
    for m in _ATTR_HINT.finditer(doc):
        out += [p.strip() for p in re.split(r"[,;]| and ", m.group(1)) if p.strip()]
    return out


# --------------------------------------------------------------------------------------------
# schema model
# --------------------------------------------------------------------------------------------


@dataclass
class Enum:
    value: str
    doc: str = ""
    #: for QName-valued enumerations: the value resolved in the scope of the enumeration element
    resolved: QName | None = None


@dataclass
class Facets:
    enumerations: list[Enum] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    length: int | None = None
    min_length: int | None = None
    max_length: int | None = None
    min_inclusive: str | None = None
    max_inclusive: str | None = None
    min_exclusive: str | None = None
    max_exclusive: str | None = None
    total_digits: int | None = None
    fraction_digits: int | None = None
    white_space: str | None = None

    def merged(self, other: "Facets") -> "Facets":
        """``other`` is the derived (more specific) restriction; its facets replace ours."""
        out = Facets(**{k: getattr(self, k) for k in self.__dataclass_fields__})
        for k in self.__dataclass_fields__:
            v = getattr(other, k)
            if k == "patterns":
                out.patterns = list(self.patterns) + list(other.patterns)
            elif k == "enumerations":
                if other.enumerations:
                    out.enumerations = list(other.enumerations)
            elif v is not None:
                setattr(out, k, v)
        return out


@dataclass
class SimpleType:
    qname: QName | None
    variety: str = "atomic"  # atomic | union | list
    base: QName | None = None
    inline_base: "SimpleType | None" = None
    members: list["QName | SimpleType"] = field(default_factory=list)
    item: "QName | SimpleType | None" = None
    facets: Facets = field(default_factory=Facets)
    doc: str = ""
    source: str = ""


@dataclass
class AnyAttr:
    namespace: str = "##any"
    process_contents: str = "strict"


@dataclass
class AttributeUse:
    name: QName
    is_ref: bool = False
    type: QName | None = None
    simple: SimpleType | None = None
    use: str = "optional"
    default: str | None = None
    fixed: str | None = None
    doc: str = ""

    @property
    def required(self) -> bool:
        return self.use == "required"


@dataclass
class Particle:
    kind: str  # element | any | sequence | choice | all | group
    name: QName | None = None
    is_ref: bool = False
    type: QName | None = None
    anon_type: "ComplexType | SimpleType | None" = None
    min_occurs: int = 1
    max_occurs: int | None = 1
    nillable: bool = False
    default: str | None = None
    fixed: str | None = None
    namespace: str = "##any"  # any only
    process_contents: str = "strict"  # any only
    children: list["Particle"] = field(default_factory=list)
    group_ref: QName | None = None
    doc: str = ""
    doc_attributes: list[str] = field(default_factory=list)
    target_ns: str = ""  # target namespace of the declaring schema (for ##other / ##targetNamespace)


@dataclass
class ComplexType:
    qname: QName | None
    abstract: bool = False
    mixed: bool = False
    content: str = "element"  # empty | simple | element
    derivation: str | None = None  # extension | restriction
    base: QName | None = None
    particle: Particle | None = None
    attributes: list[AttributeUse] = field(default_factory=list)
    attribute_groups: list[QName] = field(default_factory=list)
    any_attribute: AnyAttr | None = None
    simple_facets: Facets | None = None  # simpleContent restriction facets
    simple_inline: SimpleType | None = None
    doc: str = ""
    source: str = ""
    target_ns: str = ""


@dataclass
class Element:
    name: QName
    type: QName | None = None
    anon_type: ComplexType | SimpleType | None = None
    substitution_group: QName | None = None
    abstract: bool = False
    nillable: bool = False
    default: str | None = None
    fixed: str | None = None
    doc: str = ""
    doc_attributes: list[str] = field(default_factory=list)
    source: str = ""


@dataclass
class AttributeGroup:
    name: QName
    attributes: list[AttributeUse] = field(default_factory=list)
    groups: list[QName] = field(default_factory=list)
    any_attribute: AnyAttr | None = None
    doc: str = ""


@dataclass
class Group:
    name: QName
    particle: Particle | None = None
    doc: str = ""


@dataclass
class SchemaDoc:
    source: str
    target_ns: str
    element_form_qualified: bool
    attribute_form_qualified: bool
    nsmap: dict[str, str]
    includes: list[str] = field(default_factory=list)
    imports: list[tuple[str, str]] = field(default_factory=list)  # (namespace, location)
    doc: str = ""
    embedded_in: str | None = None  # WSDL file when this schema sits in wsdl:types


# ---- WSDL ---------------------------------------------------------------------------------


@dataclass
class Message:
    name: QName
    parts: list[tuple[str, QName | None, QName | None]] = field(default_factory=list)  # name, element, type


@dataclass
class OpMessage:
    role: str  # input | output | fault
    message: QName | None
    action: str | None = None
    name: str | None = None


@dataclass
class Operation:
    name: str
    port_type: QName
    messages: list[OpMessage] = field(default_factory=list)
    doc: str = ""
    source: str = ""

    @property
    def kind(self) -> str:
        roles = [m.role for m in self.messages if m.role != "fault"]
        if roles == ["input", "output"]:
            return "request-response"
        if roles == ["input"]:
            return "one-way"
        if roles == ["output"]:
            return "notification"
        if roles == ["output", "input"]:
            return "solicit-response"
        return "other"

    def msg(self, role: str) -> OpMessage | None:
        return next((m for m in self.messages if m.role == role), None)


@dataclass
class PortType:
    name: QName
    operations: list[Operation] = field(default_factory=list)
    event_source: bool = False
    doc: str = ""
    source: str = ""


@dataclass
class BindingOp:
    name: str
    soap_action: str | None = None
    soap_action_required: bool | None = None
    style: str | None = None
    input_use: str | None = None
    output_use: str | None = None


@dataclass
class Binding:
    name: QName
    port_type: QName
    soap_version: str | None = None  # "1.2" | "1.1"
    transport: str | None = None
    style: str | None = None
    operations: dict[str, BindingOp] = field(default_factory=dict)
    policy_refs: list[str] = field(default_factory=list)
    source: str = ""


@dataclass
class Port:
    name: str
    binding: QName
    address: str | None = None


@dataclass
class Service:
    name: str
    ports: list[Port] = field(default_factory=list)


@dataclass
class Wsdl:
    source: str
    name: str
    target_ns: str
    nsmap: dict[str, str]
    messages: dict[QName, Message] = field(default_factory=dict)
    port_types: dict[QName, PortType] = field(default_factory=dict)
    bindings: dict[QName, Binding] = field(default_factory=dict)
    services: dict[str, Service] = field(default_factory=dict)
    policies: list[str] = field(default_factory=list)
    doc: str = ""


@dataclass
class OperationView:
    """A WSDL operation joined to its message elements, soapAction and schema types."""

    name: str
    kind: str
    port_type: QName
    event_source: bool
    wsdl: str
    input_element: QName | None = None
    output_element: QName | None = None
    input_action: str | None = None
    output_action: str | None = None
    soap_action: str | None = None
    soap_action_required: bool | None = None
    fault_messages: list[QName] = field(default_factory=list)
    doc: str = ""


@dataclass
class Problem:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


# --------------------------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------------------------


class _Parser:
    """Turns xs:schema nodes into model objects stored on a :class:`SchemaSet`."""

    def __init__(self, ss: "SchemaSet", doc: SchemaDoc) -> None:
        self.ss = ss
        self.doc = doc
        self.tns = doc.target_ns

    # -- helpers
    def _elem_qname(self, node: Node, is_global: bool) -> QName:
        name = node.get("name") or ""
        if is_global:
            return (self.tns, name)
        form = node.get("form")
        qualified = self.doc.element_form_qualified if form is None else form == "qualified"
        return (self.tns if qualified else "", name)

    def _attr_qname(self, node: Node, is_global: bool) -> QName:
        name = node.get("name") or ""
        if is_global:
            return (self.tns, name)
        form = node.get("form")
        qualified = self.doc.attribute_form_qualified if form is None else form == "qualified"
        return (self.tns if qualified else "", name)

    def _type_ref(self, node: Node, attr: str = "type") -> QName | None:
        v = node.get(attr)
        return node.qname(v) if v else None

    # -- top level
    def ingest(self, root: Node) -> None:
        ss = self.ss
        self.doc.doc = _docs(root)
        for c in root.children:
            if c.ns != XS:
                continue
            kind = c.local
            if kind == "element" and c.get("name"):
                el = self.element_decl(c)
                ss._add(ss.elements, el.name, el, "element")
            elif kind == "complexType" and c.get("name"):
                ct = self.complex_type(c, (self.tns, c.get("name") or ""))
                ss._add(ss.complex_types, ct.qname, ct, "complexType")
            elif kind == "simpleType" and c.get("name"):
                st = self.simple_type(c, (self.tns, c.get("name") or ""))
                ss._add(ss.simple_types, st.qname, st, "simpleType")
            elif kind == "attribute" and c.get("name"):
                au = self.attribute_use(c, True)
                ss._add(ss.attributes, au.name, au, "attribute")
            elif kind == "attributeGroup" and c.get("name"):
                ag = AttributeGroup(name=(self.tns, c.get("name") or ""), doc=_docs(c))
                self.attribute_content(c, ag.attributes, ag.groups)
                ag.any_attribute = self.any_attribute(c)
                ss._add(ss.attribute_groups, ag.name, ag, "attributeGroup")
            elif kind == "group" and c.get("name"):
                g = Group(name=(self.tns, c.get("name") or ""), doc=_docs(c))
                for k in c.children:
                    if k.ns == XS and k.local in ("sequence", "choice", "all"):
                        g.particle = self.model_group(k)
                ss._add(ss.groups, g.name, g, "group")

    # -- elements
    def element_decl(self, node: Node) -> Element:
        doc = _docs(node)
        el = Element(
            name=(self.tns, node.get("name") or ""),
            type=self._type_ref(node),
            substitution_group=self._type_ref(node, "substitutionGroup"),
            abstract=node.get("abstract") == "true",
            nillable=node.get("nillable") == "true",
            default=node.get("default"),
            fixed=node.get("fixed"),
            doc=doc,
            doc_attributes=_doc_attribute_hints(doc),
            source=self.doc.source,
        )
        el.anon_type = self.inline_type(node)
        return el

    def inline_type(self, node: Node) -> "ComplexType | SimpleType | None":
        ct = node.kid("complexType")
        if ct is not None:
            return self.complex_type(ct, None)
        st = node.kid("simpleType")
        if st is not None:
            return self.simple_type(st, None)
        return None

    def particle_element(self, node: Node) -> Particle:
        mn, mx = _occurs(node)
        doc = _docs(node)
        p = Particle(
            kind="element", min_occurs=mn, max_occurs=mx, doc=doc,
            doc_attributes=_doc_attribute_hints(doc), nillable=node.get("nillable") == "true",
            default=node.get("default"), fixed=node.get("fixed"), target_ns=self.tns,
        )
        ref = self._type_ref(node, "ref")
        if ref is not None:
            p.name, p.is_ref = ref, True
        else:
            p.name = self._elem_qname(node, False)
            p.type = self._type_ref(node)
            p.anon_type = self.inline_type(node)
        return p

    def model_group(self, node: Node) -> Particle:
        mn, mx = _occurs(node)
        p = Particle(kind=node.local, min_occurs=mn, max_occurs=mx, target_ns=self.tns, doc=_docs(node))
        for c in node.children:
            if c.ns != XS:
                continue
            if c.local == "element":
                p.children.append(self.particle_element(c))
            elif c.local in ("sequence", "choice", "all"):
                p.children.append(self.model_group(c))
            elif c.local == "any":
                amn, amx = _occurs(c)
                p.children.append(Particle(
                    kind="any", min_occurs=amn, max_occurs=amx,
                    namespace=c.get("namespace") or "##any",
                    process_contents=c.get("processContents") or "strict",
                    target_ns=self.tns, doc=_docs(c),
                ))
            elif c.local == "group" and c.get("ref"):
                gmn, gmx = _occurs(c)
                p.children.append(Particle(kind="group", min_occurs=gmn, max_occurs=gmx,
                                           group_ref=c.qname(c.get("ref") or ""), target_ns=self.tns))
        return p

    # -- attributes
    def attribute_use(self, node: Node, is_global: bool) -> AttributeUse:
        ref = self._type_ref(node, "ref")
        au = AttributeUse(
            name=ref if ref is not None else self._attr_qname(node, is_global),
            is_ref=ref is not None,
            use=node.get("use") or "optional",
            default=node.get("default"), fixed=node.get("fixed"), doc=_docs(node),
        )
        au.type = self._type_ref(node)
        st = node.kid("simpleType")
        if st is not None:
            au.simple = self.simple_type(st, None)
        return au

    def any_attribute(self, node: Node) -> AnyAttr | None:
        aa = node.kid("anyAttribute")
        if aa is None:
            return None
        return AnyAttr(aa.get("namespace") or "##any", aa.get("processContents") or "strict")

    def attribute_content(self, node: Node, attrs: list[AttributeUse], groups: list[QName]) -> None:
        for c in node.children:
            if c.ns != XS:
                continue
            if c.local == "attribute":
                attrs.append(self.attribute_use(c, False))
            elif c.local == "attributeGroup" and c.get("ref"):
                groups.append(c.qname(c.get("ref") or ""))

    # -- complex types
    def complex_type(self, node: Node, qname: QName | None) -> ComplexType:
        ct = ComplexType(
            qname=qname, abstract=node.get("abstract") == "true", mixed=node.get("mixed") == "true",
            doc=_docs(node), source=self.doc.source, target_ns=self.tns,
        )
        sc = node.kid("simpleContent")
        cc = node.kid("complexContent")
        holder = node
        if sc is not None:
            ct.content = "simple"
            deriv = sc.kid("extension") or sc.kid("restriction")
            if deriv is not None:
                ct.derivation, holder = deriv.local, deriv
                ct.base = self._type_ref(deriv, "base")
                if deriv.local == "restriction":
                    ct.simple_facets = self.facets(deriv)
                    inl = deriv.kid("simpleType")
                    if inl is not None:
                        ct.simple_inline = self.simple_type(inl, None)
        elif cc is not None:
            ct.mixed = ct.mixed or cc.get("mixed") == "true"
            deriv = cc.kid("extension") or cc.kid("restriction")
            if deriv is not None:
                ct.derivation, holder = deriv.local, deriv
                ct.base = self._type_ref(deriv, "base")
        if sc is None:
            for k in holder.children:
                if k.ns == XS and k.local in ("sequence", "choice", "all"):
                    ct.particle = self.model_group(k)
                elif k.ns == XS and k.local == "group" and k.get("ref"):
                    mn, mx = _occurs(k)
                    ct.particle = Particle(kind="group", min_occurs=mn, max_occurs=mx,
                                           group_ref=k.qname(k.get("ref") or ""), target_ns=self.tns)
            if ct.particle is None and ct.derivation is None:
                ct.content = "empty"
        self.attribute_content(holder, ct.attributes, ct.attribute_groups)
        ct.any_attribute = self.any_attribute(holder)
        return ct

    # -- simple types
    def facets(self, restriction: Node) -> Facets:
        f = Facets()
        for c in restriction.children:
            if c.ns != XS:
                continue
            v = c.get("value")
            if c.local == "enumeration" and v is not None:
                f.enumerations.append(Enum(v, _docs(c), c.qname(v) if ":" in v else None))
            elif c.local == "pattern" and v is not None:
                f.patterns.append(v)
            elif c.local in ("length", "minLength", "maxLength", "totalDigits", "fractionDigits") and v is not None:
                try:
                    n = int(v)
                except ValueError:
                    continue
                setattr(f, {"length": "length", "minLength": "min_length", "maxLength": "max_length",
                            "totalDigits": "total_digits", "fractionDigits": "fraction_digits"}[c.local], n)
            elif c.local in ("minInclusive", "maxInclusive", "minExclusive", "maxExclusive") and v is not None:
                setattr(f, {"minInclusive": "min_inclusive", "maxInclusive": "max_inclusive",
                            "minExclusive": "min_exclusive", "maxExclusive": "max_exclusive"}[c.local], v)
            elif c.local == "whiteSpace" and v is not None:
                f.white_space = v
        return f

    def simple_type(self, node: Node, qname: QName | None) -> SimpleType:
        st = SimpleType(qname=qname, doc=_docs(node), source=self.doc.source)
        union = node.kid("union")
        lst = node.kid("list")
        res = node.kid("restriction")
        if union is not None:
            st.variety = "union"
            for m in (union.get("memberTypes") or "").split():
                st.members.append(union.qname(m))
            for inl in union.kids("simpleType"):
                st.members.append(self.simple_type(inl, None))
        elif lst is not None:
            st.variety = "list"
            it = self._type_ref(lst, "itemType")
            inl = lst.kid("simpleType")
            st.item = it if it is not None else (self.simple_type(inl, None) if inl is not None else None)
        elif res is not None:
            st.base = self._type_ref(res, "base")
            inl = res.kid("simpleType")
            if inl is not None:
                st.inline_base = self.simple_type(inl, None)
            st.facets = self.facets(res)
        return st


# --------------------------------------------------------------------------------------------
# the schema set
# --------------------------------------------------------------------------------------------


class SchemaSet:
    """All schema documents and WSDL files of one service package, plus lookups and views."""

    def __init__(self) -> None:
        self.docs: list[SchemaDoc] = []
        self.wsdls: list[Wsdl] = []
        self.elements: dict[QName, Element] = {}
        self.complex_types: dict[QName, ComplexType] = {}
        self.simple_types: dict[QName, SimpleType] = {}
        self.attributes: dict[QName, AttributeUse] = {}
        self.attribute_groups: dict[QName, AttributeGroup] = {}
        self.groups: dict[QName, Group] = {}
        self.unresolved: list[tuple[str, str, str]] = []  # (from file, kind, location/namespace)
        self.duplicates: list[tuple[str, QName, str]] = []  # (kind, qname, source)
        self.prefixes: dict[str, str] = {}  # namespace uri -> preferred prefix
        self._seen: set[Path] = set()
        self._prefix_votes: dict[str, dict[str, int]] = {}
        self._anon = itertools.count(1)

    # ---- loading -------------------------------------------------------------------------
    def _add(self, table: dict, key: QName, value: Any, kind: str) -> None:
        if key in table:
            self.duplicates.append((kind, key, getattr(value, "source", "")))
            return
        table[key] = value

    def add_path(self, path: Path | str, *, search_dirs: Iterable[Path | str] = (),
                 chameleon_ns: str | None = None, embedded_in: str | None = None) -> None:
        p = Path(path)
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.suffix.lower() in (".xsd", ".wsdl"):
                    self.add_path(f, search_dirs=search_dirs)
            return
        key = p.resolve()
        if key in self._seen:
            return
        self._seen.add(key)
        text = p.read_bytes()
        self.add_text(text, source=str(p), search_dirs=[*map(Path, search_dirs)],
                      base=p.parent, chameleon_ns=chameleon_ns)

    def add_text(self, text: str | bytes, source: str = "<string>", *, base: Path | None = None,
                 search_dirs: Iterable[Path] = (), chameleon_ns: str | None = None) -> None:
        root = parse_xml(text, source)
        if root.ns == XS and root.local == "schema":
            self._add_schema(root, source, base, list(search_dirs), chameleon_ns, None)
        elif root.ns == WSDL and root.local == "definitions":
            self._add_wsdl(root, source, base, list(search_dirs))
        else:
            raise ValueError(f"{source}: not an XSD or WSDL document (root {root.local})")

    def _vote(self, nsmap: dict[str, str]) -> None:
        for prefix, uri in nsmap.items():
            if prefix and uri not in (XS, XML_NS):
                self._prefix_votes.setdefault(uri, {}).setdefault(prefix, 0)
                self._prefix_votes[uri][prefix] += 1
        for uri, votes in self._prefix_votes.items():
            self.prefixes[uri] = max(votes.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]

    def _add_schema(self, root: Node, source: str, base: Path | None, search: list[Path],
                    chameleon_ns: str | None, embedded_in: str | None) -> None:
        tns = root.get("targetNamespace")
        if tns is None:
            tns = chameleon_ns or ""
        doc = SchemaDoc(
            source=source, target_ns=tns,
            element_form_qualified=root.get("elementFormDefault") == "qualified",
            attribute_form_qualified=root.get("attributeFormDefault") == "qualified",
            nsmap=dict(root.scope), embedded_in=embedded_in,
        )
        self.docs.append(doc)
        self._vote(root.scope)
        _Parser(self, doc).ingest(root)
        for c in root.kids("include"):
            loc = c.get("schemaLocation")
            if loc:
                doc.includes.append(loc)
                self._follow(loc, source, base, search, chameleon_ns=tns)
        for c in root.kids("import"):
            loc = c.get("schemaLocation")
            ns = c.get("namespace") or ""
            doc.imports.append((ns, loc or ""))
            if loc:
                self._follow(loc, source, base, search, chameleon_ns=None)
            else:
                self.unresolved.append((source, "import-no-location", ns))
        for c in root.kids("redefine"):
            self.unresolved.append((source, "redefine-unsupported", c.get("schemaLocation") or ""))

    def _follow(self, loc: str, source: str, base: Path | None, search: list[Path],
                chameleon_ns: str | None) -> None:
        if loc.startswith(("http://", "https://")):
            self.unresolved.append((source, "remote-location", loc))
            return
        candidates: list[Path] = []
        if base is not None:
            candidates.append(base / loc)
        for d in search:
            candidates.append(d / loc)
            candidates.append(d / Path(loc).name)
        for cand in candidates:
            if cand.is_file():
                self.add_path(cand, search_dirs=search, chameleon_ns=chameleon_ns)
                return
        self.unresolved.append((source, "location-not-found", loc))

    # ---- WSDL ----------------------------------------------------------------------------
    def _add_wsdl(self, root: Node, source: str, base: Path | None, search: list[Path]) -> None:
        tns = root.get("targetNamespace") or ""
        w = Wsdl(source=source, name=root.get("name") or "", target_ns=tns, nsmap=dict(root.scope),
                 doc=_docs_wsdl(root))
        self._vote(root.scope)
        self.wsdls.append(w)
        for t in root.kids("types", WSDL):
            for sch in t.kids("schema"):
                self._add_schema(sch, source, base, search, None, source)
        for pol in root.kids("Policy", WSP):
            pid = pol.get("Id", WSU)
            if pid:
                w.policies.append(pid)
        for m in root.kids("message", WSDL):
            msg = Message(name=(tns, m.get("name") or ""))
            for part in m.kids("part", WSDL):
                el = part.get("element")
                ty = part.get("type")
                msg.parts.append((part.get("name") or "", part.qname(el) if el else None,
                                  part.qname(ty) if ty else None))
            w.messages[msg.name] = msg
        for pt in root.kids("portType", WSDL):
            ptype = PortType(
                name=(tns, pt.get("name") or ""),
                event_source=(pt.get("EventSource", WSE) or "").lower() in ("true", "1"),
                doc=_docs_wsdl(pt), source=source,
            )
            for op in pt.kids("operation", WSDL):
                o = Operation(name=op.get("name") or "", port_type=ptype.name,
                              doc=_docs_wsdl(op), source=source)
                for io in op.children:
                    if io.ns != WSDL or io.local not in ("input", "output", "fault"):
                        continue
                    mref = io.get("message")
                    action = io.get("Action", WSA) or io.get("Action", WSAM)
                    o.messages.append(OpMessage(role=io.local, message=io.qname(mref) if mref else None,
                                                action=action, name=io.get("name")))
                ptype.operations.append(o)
            w.port_types[ptype.name] = ptype
        for b in root.kids("binding", WSDL):
            bd = Binding(name=(tns, b.get("name") or ""), port_type=b.qname(b.get("type") or ""), source=source)
            for sb in b.children:
                if sb.local == "binding" and sb.ns in (WSDL_SOAP12, "http://schemas.xmlsoap.org/wsdl/soap/"):
                    bd.soap_version = "1.2" if sb.ns == WSDL_SOAP12 else "1.1"
                    bd.transport = sb.get("transport")
                    bd.style = sb.get("style")
                elif sb.local == "PolicyReference" and sb.ns == WSP:
                    bd.policy_refs.append(sb.get("URI") or "")
            for op in b.kids("operation", WSDL):
                bo = BindingOp(name=op.get("name") or "")
                for sop in op.children:
                    if sop.local == "operation" and sop.ns in (WSDL_SOAP12, "http://schemas.xmlsoap.org/wsdl/soap/"):
                        bo.soap_action = sop.get("soapAction")
                        sar = sop.get("soapActionRequired")
                        bo.soap_action_required = None if sar is None else sar == "true"
                        bo.style = sop.get("style")
                for io in op.children:
                    if io.ns == WSDL and io.local in ("input", "output"):
                        body = next((x for x in io.children if x.local == "body"), None)
                        use = body.get("use") if body is not None else None
                        if io.local == "input":
                            bo.input_use = use
                        else:
                            bo.output_use = use
                bd.operations[bo.name] = bo
            w.bindings[bd.name] = bd
        for s in root.kids("service", WSDL):
            svc = Service(name=s.get("name") or "")
            for pnode in s.kids("port", WSDL):
                addr = next((x.get("location") for x in pnode.children if x.local == "address"), None)
                svc.ports.append(Port(pnode.get("name") or "", pnode.qname(pnode.get("binding") or ""), addr))
            w.services[svc.name] = svc

    # ---- names ---------------------------------------------------------------------------
    def qstr(self, qn: QName | None) -> str:
        """``prefix:local`` for display (prefix from the package; ``xs:`` for XSD built-ins)."""
        if qn is None:
            return ""
        ns, local = qn
        if ns == XS:
            return f"xs:{local}"
        if not ns:
            return local
        prefix = self.prefixes.get(ns)
        return f"{prefix}:{local}" if prefix else f"{{{ns}}}{local}"

    def namespaces(self) -> dict[str, str]:
        """prefix -> namespace uri, as chosen for display."""
        out = {p: u for u, p in self.prefixes.items()}
        out.setdefault("xs", XS)
        return dict(sorted(out.items()))

    def target_namespaces(self) -> list[str]:
        seen: list[str] = []
        for d in self.docs:
            if d.target_ns and d.target_ns not in seen:
                seen.append(d.target_ns)
        return seen

    # ---- type resolution -----------------------------------------------------------------
    def lookup_type(self, qn: QName | None) -> "ComplexType | SimpleType | None":
        if qn is None:
            return None
        return self.complex_types.get(qn) or self.simple_types.get(qn)

    def is_builtin(self, qn: QName | None) -> bool:
        return qn is not None and qn[0] == XS

    def builtin_kind(self, qn: QName) -> str:
        return _BUILTIN_KINDS.get(qn[1], "string")

    def ancestry(self, ct: ComplexType) -> list[ComplexType]:
        """Base-first chain of complex types ending with ``ct``."""
        chain = [ct]
        seen = {id(ct)}
        cur = ct
        while cur.base is not None and not self.is_builtin(cur.base):
            nxt = self.complex_types.get(cur.base)
            if nxt is None or id(nxt) in seen:
                break
            chain.append(nxt)
            seen.add(id(nxt))
            cur = nxt
        return list(reversed(chain))

    def element_type(self, el: "Element | Particle") -> "ComplexType | SimpleType | None":
        if el.anon_type is not None:
            return el.anon_type
        return self.lookup_type(el.type)

    def _deref_particle_element(self, p: Particle) -> tuple[QName | None, "ComplexType | SimpleType | None", Element | None]:
        """For an element particle (possibly a ref) -> (type qname, type object, global decl)."""
        if p.is_ref and p.name is not None:
            g = self.elements.get(p.name)
            if g is None:
                return None, None, None
            return g.type, self.element_type(g), g
        return p.type, self.element_type(p), None

    def effective_particles(self, ct: ComplexType) -> list[tuple[Particle, QName | None]]:
        """Flattened element/any particles (base first) with the type that declares each.

        ``Particle.min_occurs`` of a row already folds in enclosing sequence/choice minimums;
        rows inside a choice get ``min_occurs == 0`` and the ``choice`` marker in ``rows``.
        Use :meth:`effective_rows` for the documentation friendly structure.
        """
        return [(r.particle, r.declared_in) for r in self.effective_rows(ct) if r.particle is not None]

    def effective_rows(self, ct: ComplexType) -> list["Row"]:
        rows: list[Row] = []
        for t in self.ancestry(ct):
            if t.particle is not None:
                self._flatten(t.particle, 1, 1, False, None, t.qname, rows, set())
        return rows

    def _flatten(self, p: Particle, mn: int, mx: int | None, in_choice: bool, choice_id: str | None,
                 declared_in: QName | None, rows: list["Row"], visiting: set[QName]) -> None:
        pmn = mn * p.min_occurs
        pmx = None if (mx is None or p.max_occurs is None) else mx * p.max_occurs
        if p.kind in ("element", "any"):
            rows.append(Row(p, 0 if in_choice else pmn, pmx, choice_id, declared_in))
        elif p.kind in ("sequence", "all"):
            for c in p.children:
                self._flatten(c, pmn, pmx, in_choice, choice_id, declared_in, rows, visiting)
        elif p.kind == "choice":
            cid = f"choice{len(rows)}_{next(self._anon)}"
            for c in p.children:
                self._flatten(c, pmn, pmx, True, cid, declared_in, rows, visiting)
        elif p.kind == "group" and p.group_ref is not None and p.group_ref not in visiting:
            g = self.groups.get(p.group_ref)
            if g is not None and g.particle is not None:
                self._flatten(g.particle, pmn, pmx, in_choice, choice_id, declared_in, rows, visiting | {p.group_ref})

    def effective_attributes(self, ct: ComplexType) -> list[tuple[AttributeUse, QName | None]]:
        """Attributes of ``ct`` including inherited and attributeGroup members (later wins)."""
        out: dict[QName, tuple[AttributeUse, QName | None]] = {}
        for t in self.ancestry(ct):
            for a in t.attributes:
                out[a.name] = (a, t.qname)
            for g in t.attribute_groups:
                for a in self._group_attrs(g, set()):
                    out[a.name] = (a, g)
        return list(out.values())

    def _group_attrs(self, qn: QName, seen: set[QName]) -> list[AttributeUse]:
        g = self.attribute_groups.get(qn)
        if g is None or qn in seen:
            return []
        out = list(g.attributes)
        for sub in g.groups:
            out += self._group_attrs(sub, seen | {qn})
        return out

    def effective_any_attribute(self, ct: ComplexType) -> AnyAttr | None:
        res = None
        for t in self.ancestry(ct):
            if t.any_attribute is not None:
                res = t.any_attribute
            for g in t.attribute_groups:
                ag = self.attribute_groups.get(g)
                if ag is not None and ag.any_attribute is not None:
                    res = ag.any_attribute
        return res

    def attribute_type(self, a: AttributeUse) -> "QName | SimpleType | None":
        if a.simple is not None:
            return a.simple
        if a.type is not None:
            return a.type
        if a.is_ref:
            g = self.attributes.get(a.name)
            if g is not None:
                return g.simple if g.simple is not None else g.type
        return None

    def attribute_default(self, a: AttributeUse) -> str | None:
        if a.default is not None or a.fixed is not None or not a.is_ref:
            return a.default if a.default is not None else a.fixed
        g = self.attributes.get(a.name)
        if g is not None:
            return g.default if g.default is not None else g.fixed
        return None

    # -- simple value info
    def simple_info(self, t: "QName | SimpleType | ComplexType | None", _depth: int = 0) -> "SimpleInfo":
        """Resolve any simple-valued type (or simpleContent complex type) to its value space."""
        info = SimpleInfo()
        if t is None or _depth > 24:
            return info
        if isinstance(t, tuple):
            if self.is_builtin(t):
                info.builtin = t[1]
                info.kind = self.builtin_kind(t)
                info.chain.append(self.qstr(t))
                return info
            obj = self.lookup_type(t)
            if obj is None:
                info.chain.append(self.qstr(t) + " (unresolved)")
                return info
            return self.simple_info(obj, _depth + 1)
        if isinstance(t, ComplexType):
            if t.content != "simple":
                info.kind = "complex"
                return info
            base_info = SimpleInfo()
            for ancestor in self.ancestry(t):
                if ancestor.base is not None:
                    base_info = self.simple_info(ancestor.base, _depth + 1) if not isinstance(
                        self.lookup_type(ancestor.base), ComplexType) or ancestor is t else base_info
            # simple content: value space is the (possibly complex-typed) base
            b = self.lookup_type(t.base) if t.base else None
            if isinstance(b, ComplexType):
                base_info = self.simple_info(b, _depth + 1)
            elif t.base is not None:
                base_info = self.simple_info(t.base, _depth + 1)
            if t.simple_facets is not None:
                base_info.facets = base_info.facets.merged(t.simple_facets)
            if t.qname:
                base_info.chain.insert(0, self.qstr(t.qname))
            return base_info
        # SimpleType
        if t.variety == "union":
            info.kind = "union"
            for m in t.members:
                sub = self.simple_info(m, _depth + 1)
                info.members.append(sub)
            info.chain.append(self.qstr(t.qname) if t.qname else "(anonymous union)")
            info.facets = t.facets
            return info
        if t.variety == "list":
            info.kind = "list"
            info.item = self.simple_info(t.item, _depth + 1)
            info.chain.append(self.qstr(t.qname) if t.qname else "(anonymous list)")
            return info
        base = t.inline_base if t.inline_base is not None else t.base
        sub = self.simple_info(base, _depth + 1) if base is not None else SimpleInfo()
        sub.facets = sub.facets.merged(t.facets)
        sub.chain.insert(0, self.qstr(t.qname) if t.qname else "(anonymous)")
        return sub

    def enumeration(self, t: "QName | SimpleType | ComplexType | None") -> list[tuple[str, str, str]]:
        """Flattened enumerations (value, doc, declaring-type) including union members."""
        out: list[tuple[str, str, str]] = []
        self._enum(t, out, 0)
        return out

    def _enum(self, t: Any, out: list[tuple[str, str, str]], depth: int) -> None:
        if t is None or depth > 24:
            return
        if isinstance(t, tuple):
            obj = self.lookup_type(t)
            if obj is not None:
                self._enum(obj, out, depth + 1)
            return
        if isinstance(t, ComplexType):
            if t.content == "simple" and t.base is not None:
                self._enum(t.base, out, depth + 1)
            if t.simple_facets:
                for e in t.simple_facets.enumerations:
                    out.append((e.value, e.doc, self.qstr(t.qname)))
            return
        if t.variety == "union":
            for m in t.members:
                self._enum(m, out, depth + 1)
            return
        if t.variety == "list":
            self._enum(t.item, out, depth + 1)
            return
        if t.facets.enumerations:
            for e in t.facets.enumerations:
                out.append((e.value, e.doc, self.qstr(t.qname) if t.qname else "(anonymous)"))
            return
        base = t.inline_base if t.inline_base is not None else t.base
        self._enum(base, out, depth + 1)

    def patterns(self, t: Any) -> list[str]:
        info = self.simple_info(t)
        return _collect_patterns(info)

    # ---- substitution groups -------------------------------------------------------------
    def substitution_members(self, head: QName) -> list[QName]:
        out: list[QName] = []
        todo = [head]
        while todo:
            cur = todo.pop()
            for qn, el in self.elements.items():
                if el.substitution_group == cur and qn not in out:
                    out.append(qn)
                    todo.append(qn)
        return out

    def substitution_groups(self) -> dict[QName, list[QName]]:
        heads = {el.substitution_group for el in self.elements.values() if el.substitution_group}
        return {h: self.substitution_members(h) for h in sorted(heads)}  # type: ignore[arg-type]

    # ---- reverse index -------------------------------------------------------------------
    def usages(self) -> dict[QName, list[tuple[str, str]]]:
        """type/element qname -> [(kind, user)] where user is e.g. ``wprt:Foo/Bar``."""
        use: dict[QName, list[tuple[str, str]]] = {}

        def add(target: QName | None, kind: str, user: str) -> None:
            if target is not None and not self.is_builtin(target):
                use.setdefault(target, []).append((kind, user))

        def walk(p: Particle, owner: str) -> None:
            if p.kind == "element":
                label = f"{owner}/{self.qstr(p.name)}"
                add(p.type, "element", label)
                if p.is_ref:
                    add(p.name, "element-ref", label)
            for c in p.children:
                walk(c, owner)

        for qn, ct in self.complex_types.items():
            owner = self.qstr(qn)
            add(ct.base, "base", owner)
            if ct.particle is not None:
                walk(ct.particle, owner)
            for a in ct.attributes:
                add(a.type, "attribute", f"{owner}/@{self.qstr(a.name)}")
                if a.is_ref:
                    add(a.name, "attribute-ref", f"{owner}/@{self.qstr(a.name)}")
        for qn, el in self.elements.items():
            add(el.type, "element-decl", self.qstr(qn))
            if isinstance(el.anon_type, ComplexType) and el.anon_type.particle is not None:
                walk(el.anon_type.particle, self.qstr(qn))
        for qn, st in self.simple_types.items():
            add(st.base, "base", self.qstr(qn))
            for m in st.members:
                if isinstance(m, tuple):
                    add(m, "union-member", self.qstr(qn))
        return use

    # ---- WSDL views ----------------------------------------------------------------------
    def operations(self) -> list[OperationView]:
        views: list[OperationView] = []
        for w in self.wsdls:
            for ptype in w.port_types.values():
                bindings = [b for b in w.bindings.values() if b.port_type == ptype.name]
                for op in ptype.operations:
                    v = OperationView(name=op.name, kind=op.kind, port_type=ptype.name,
                                      event_source=ptype.event_source, wsdl=w.source, doc=op.doc)
                    for m in op.messages:
                        msg = w.messages.get(m.message) if m.message else None
                        el = next((p[1] for p in (msg.parts if msg else []) if p[1]), None)
                        if m.role == "input":
                            v.input_element, v.input_action = el, m.action
                        elif m.role == "output":
                            v.output_element, v.output_action = el, m.action
                        elif m.message:
                            v.fault_messages.append(m.message)
                    for b in bindings:
                        bo = b.operations.get(op.name)
                        if bo is not None:
                            v.soap_action = bo.soap_action
                            v.soap_action_required = bo.soap_action_required
                            break
                    views.append(v)
        return views

    def orphan_message_elements(self) -> list[QName]:
        """Global elements that look like messages (Request/Response/Event) but no WSDL uses.

        Extension schemas often ship message elements without a WSDL (or with a broken one);
        callers should treat their actions as *unknown* rather than invent them.
        """
        used: set[QName] = set()
        for v in self.operations():
            used.update(x for x in (v.input_element, v.output_element) if x)
        pat = re.compile(r"(Request|Response|Event)$")
        return sorted(q for q in self.elements if pat.search(q[1]) and q not in used)

    def fault_enumerations(self) -> list[tuple[QName, list[tuple[QName, str]]]]:
        """simpleTypes that restrict ``xs:QName`` to an enumeration of prefixed names (fault codes)."""
        out = []
        for qn, st in self.simple_types.items():
            if st.base == (XS, "QName") and st.facets.enumerations:
                vals: list[tuple[QName, str]] = []
                ok = True
                for e in st.facets.enumerations:
                    if e.resolved is None:
                        ok = False
                        break
                    vals.append((e.resolved, e.doc))
                if ok:
                    out.append((qn, vals))
        return out

    def extension_points(self) -> list[tuple[str, str, str, str]]:
        """(owner, 'element'|'attribute', namespace token, processContents) for every wildcard."""
        out: list[tuple[str, str, str, str]] = []

        def walk(p: Particle, owner: str) -> None:
            if p.kind == "any":
                out.append((owner, "element", p.namespace, p.process_contents))
            for c in p.children:
                walk(c, owner)

        for qn, ct in sorted(self.complex_types.items()):
            if ct.particle is not None:
                walk(ct.particle, self.qstr(qn))
            if ct.any_attribute is not None:
                out.append((self.qstr(qn), "attribute", ct.any_attribute.namespace, ct.any_attribute.process_contents))
        return out

    def foreign_namespaces(self) -> dict[str, list[str]]:
        """Namespaces mentioned (imports, QName type refs, wildcards) that this package does not define."""
        defined = set(self.target_namespaces())
        mentioned: dict[str, list[str]] = {}
        for d in self.docs:
            for ns, _loc in d.imports:
                if ns and ns not in defined:
                    mentioned.setdefault(ns, []).append(f"import in {Path(d.source).name}")
        for w in self.wsdls:
            for ptype in w.port_types.values():
                for op in ptype.operations:
                    for m in op.messages:
                        if m.message and m.message[0] and m.message[0] not in defined and m.message[0] != w.target_ns:
                            mentioned.setdefault(m.message[0], []).append(f"message {op.name}")
        return mentioned

    # ---- JSON ----------------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        q = self.qstr

        def simple(st: SimpleType) -> dict[str, Any]:
            return {
                "name": q(st.qname) if st.qname else None,
                "variety": st.variety,
                "base": q(st.base) if st.base else (simple(st.inline_base) if st.inline_base else None),
                "members": [q(m) if isinstance(m, tuple) else simple(m) for m in st.members],
                "item": (q(st.item) if isinstance(st.item, tuple) else simple(st.item)) if st.item else None,
                "facets": _facets_dict(st.facets),
                "doc": st.doc,
                "source": Path(st.source).name,
            }

        def particle(p: Particle) -> dict[str, Any]:
            d: dict[str, Any] = {"kind": p.kind, "min": p.min_occurs,
                                 "max": "unbounded" if p.max_occurs is None else p.max_occurs}
            if p.kind == "element":
                d.update(name=q(p.name), ref=p.is_ref, type=q(p.type) if p.type else None,
                         nillable=p.nillable or None, default=p.default, fixed=p.fixed)
                if p.anon_type is not None:
                    d["anon_type"] = complex_(p.anon_type) if isinstance(p.anon_type, ComplexType) else simple(p.anon_type)
                if p.doc:
                    d["doc"] = p.doc
                if p.doc_attributes:
                    d["doc_attributes"] = p.doc_attributes
            elif p.kind == "any":
                d.update(namespace=p.namespace, process_contents=p.process_contents)
            elif p.kind == "group":
                d["group"] = q(p.group_ref)
            else:
                d["children"] = [particle(c) for c in p.children]
            return d

        def attr(a: AttributeUse) -> dict[str, Any]:
            return {"name": q(a.name), "ref": a.is_ref, "use": a.use,
                    "type": q(a.type) if a.type else (simple(a.simple) if a.simple else None),
                    "default": a.default, "fixed": a.fixed, "doc": a.doc or None}

        def complex_(ct: ComplexType) -> dict[str, Any]:
            return {
                "name": q(ct.qname) if ct.qname else None, "abstract": ct.abstract, "mixed": ct.mixed,
                "content": ct.content, "derivation": ct.derivation, "base": q(ct.base) if ct.base else None,
                "particle": particle(ct.particle) if ct.particle else None,
                "attributes": [attr(a) for a in ct.attributes],
                "attribute_groups": [q(g) for g in ct.attribute_groups],
                "any_attribute": None if ct.any_attribute is None else
                {"namespace": ct.any_attribute.namespace, "process_contents": ct.any_attribute.process_contents},
                "simple_facets": _facets_dict(ct.simple_facets) if ct.simple_facets else None,
                "doc": ct.doc, "source": Path(ct.source).name,
            }

        ops = []
        for v in self.operations():
            ops.append({
                "name": v.name, "kind": v.kind, "port_type": q(v.port_type), "event_source": v.event_source,
                "input": q(v.input_element) if v.input_element else None,
                "output": q(v.output_element) if v.output_element else None,
                "input_action": v.input_action, "output_action": v.output_action,
                "soap_action": v.soap_action, "soap_action_required": v.soap_action_required,
                "faults": [q(f) for f in v.fault_messages], "wsdl": Path(v.wsdl).name,
            })
        return {
            "namespaces": self.namespaces(),
            "target_namespaces": self.target_namespaces(),
            "files": [{"source": Path(d.source).name, "target_ns": d.target_ns, "includes": d.includes,
                       "imports": [{"namespace": n, "location": l} for n, l in d.imports],
                       "embedded_in": Path(d.embedded_in).name if d.embedded_in else None} for d in self.docs],
            "wsdl": [{"source": Path(w.source).name, "name": w.name, "target_ns": w.target_ns,
                      "port_types": [q(p) for p in w.port_types], "bindings": [q(b) for b in w.bindings],
                      "services": {n: [{"port": p.name, "binding": q(p.binding), "address": p.address}
                                       for p in s.ports] for n, s in w.services.items()},
                      "policies": w.policies} for w in self.wsdls],
            "operations": ops,
            "elements": {q(k): {"type": q(e.type) if e.type else None,
                                "anon_type": (complex_(e.anon_type) if isinstance(e.anon_type, ComplexType)
                                              else simple(e.anon_type)) if e.anon_type else None,
                                "substitution_group": q(e.substitution_group) if e.substitution_group else None,
                                "abstract": e.abstract, "doc": e.doc, "source": Path(e.source).name}
                         for k, e in sorted(self.elements.items())},
            "complex_types": {q(k): complex_(v) for k, v in sorted(self.complex_types.items())},
            "simple_types": {q(k): simple(v) for k, v in sorted(self.simple_types.items())},
            "attributes": {q(k): attr(v) for k, v in sorted(self.attributes.items())},
            "attribute_groups": {q(k): {"attributes": [attr(a) for a in v.attributes], "groups": [q(g) for g in v.groups]}
                                 for k, v in sorted(self.attribute_groups.items())},
            "groups": {q(k): (particle(v.particle) if v.particle else None) for k, v in sorted(self.groups.items())},
            "faults": [{"type": q(t), "values": [{"code": q(c), "doc": d} for c, d in vals]}
                       for t, vals in self.fault_enumerations()],
            "substitution_groups": {q(h): [q(m) for m in ms] for h, ms in self.substitution_groups().items()},
            "unresolved": [{"from": Path(a).name, "kind": b, "target": c} for a, b, c in self.unresolved],
            "duplicates": [{"kind": a, "name": q(b), "source": Path(c).name} for a, b, c in self.duplicates],
        }


def _docs_wsdl(node: Node) -> str:
    parts = []
    for d in node.kids("documentation", WSDL):
        t = _clean_doc(d.alltext)
        if t:
            parts.append(t)
    for ann in node.kids("annotation"):
        for d in ann.kids("documentation"):
            t = _clean_doc(d.alltext)
            if t:
                parts.append(t)
    return "\n\n".join(parts)


def _facets_dict(f: Facets) -> dict[str, Any]:
    d: dict[str, Any] = {}
    if f.enumerations:
        d["enumerations"] = [{"value": e.value, **({"doc": e.doc} if e.doc else {})} for e in f.enumerations]
    for k in ("patterns", "length", "min_length", "max_length", "min_inclusive", "max_inclusive",
              "min_exclusive", "max_exclusive", "total_digits", "fraction_digits", "white_space"):
        v = getattr(f, k)
        if v not in (None, []):
            d[k] = v
    return d


@dataclass
class Row:
    """One flattened element/any particle of an effective content model."""

    particle: Particle
    min_occurs: int
    max_occurs: int | None
    choice: str | None
    declared_in: QName | None


@dataclass
class SimpleInfo:
    kind: str = "string"  # string nmtoken boolean int number dateTime anyURI QName base64 ... | union | list | complex
    builtin: str | None = None
    facets: Facets = field(default_factory=Facets)
    members: list["SimpleInfo"] = field(default_factory=list)
    item: "SimpleInfo | None" = None
    chain: list[str] = field(default_factory=list)

    def label(self) -> str:
        if self.kind == "union":
            return " | ".join(m.label() for m in self.members)
        if self.kind == "list":
            return f"list of {self.item.label() if self.item else '?'}"
        base = self.builtin or self.kind
        bits = []
        f = self.facets
        if f.length is not None:
            bits.append(f"length={f.length}")
        if f.min_length is not None:
            bits.append(f"minLength={f.min_length}")
        if f.max_length is not None:
            bits.append(f"maxLength={f.max_length}")
        if f.min_inclusive is not None:
            bits.append(f">={f.min_inclusive}")
        if f.max_inclusive is not None:
            bits.append(f"<={f.max_inclusive}")
        if f.min_exclusive is not None:
            bits.append(f">{f.min_exclusive}")
        if f.max_exclusive is not None:
            bits.append(f"<{f.max_exclusive}")
        if f.enumerations:
            bits.append(f"{len(f.enumerations)} values")
        if f.patterns:
            bits.append(f"{len(f.patterns)} pattern(s)")
        return base + (f" ({', '.join(bits)})" if bits else "")


def _collect_patterns(info: SimpleInfo) -> list[str]:
    out = list(info.facets.patterns)
    for m in info.members:
        out += _collect_patterns(m)
    if info.item:
        out += _collect_patterns(info.item)
    return out


def load(paths: Iterable[Path | str], *, search_dirs: Iterable[Path | str] = ()) -> SchemaSet:
    """Build a :class:`SchemaSet` from files and/or directories (xsd and wsdl, non-recursive)."""
    ss = SchemaSet()
    sd = [Path(s) for s in search_dirs]
    for p in paths:
        ss.add_path(p, search_dirs=sd)
    return ss


# --------------------------------------------------------------------------------------------
# instance validation
# --------------------------------------------------------------------------------------------

_RE_CACHE: dict[str, re.Pattern[str] | None] = {}


def _xsd_regex(pattern: str) -> re.Pattern[str] | None:
    if pattern in _RE_CACHE:
        return _RE_CACHE[pattern]
    py = pattern.replace(r"\i", "[A-Za-z_:]").replace(r"\c", r"[\w:.\-]")
    try:
        rx: re.Pattern[str] | None = re.compile(rf"^(?:{py})$", re.S)
    except re.error:
        rx = None
    _RE_CACHE[pattern] = rx
    return rx


_DATETIME = re.compile(r"^-?\d{4,}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
_BASE64 = re.compile(r"^[A-Za-z0-9+/=\s]*$")
_URI = re.compile(r"^[^\s]*$")


class _Validator:
    def __init__(self, ss: SchemaSet, ns_alias: dict[str, str], strict_any: bool) -> None:
        self.ss = ss
        self.alias = ns_alias
        self.strict_any = strict_any
        self.problems: list[Problem] = []

    def err(self, path: str, msg: str) -> None:
        self.problems.append(Problem(path, msg))

    def canon(self, qn: QName) -> QName:
        return (self.alias.get(qn[0], qn[0]), qn[1])

    # -- simple values
    def check_simple(self, info: SimpleInfo, text: str, path: str, *, report: bool = True) -> bool:
        errs = self._simple_errors(info, text)
        if errs and report:
            self.err(path, f"value {_short(text)!r} invalid: {errs[0]}")
        return not errs

    def _simple_errors(self, info: SimpleInfo, text: str) -> list[str]:
        if info.kind == "complex":
            return []
        if info.kind == "union":
            all_errs: list[str] = []
            for m in info.members:
                e = self._simple_errors(m, text)
                if not e:
                    return self._facet_errors(info.facets, text) if (info.facets.enumerations or info.facets.patterns) else []
                all_errs += e
            return [f"matches no union member ({'; '.join(sorted(set(all_errs))[:2])})"]
        if info.kind == "list":
            errs = []
            for tok in text.split():
                if info.item is not None:
                    errs += self._simple_errors(info.item, tok)
            return errs
        v = text if info.facets.white_space == "preserve" else text.strip()
        errs = self._kind_errors(info, v)
        if not errs:
            errs = self._facet_errors(info.facets, v)
        return errs

    def _kind_errors(self, info: SimpleInfo, v: str) -> list[str]:
        k = info.kind
        if k == "boolean":
            return [] if v in ("true", "false", "1", "0") else ["not a boolean"]
        if k == "int":
            try:
                n = int(v)
            except ValueError:
                return ["not an integer"]
            lo, hi = _INT_RANGES.get(info.builtin or "", (None, None))
            if (lo is not None and n < lo) or (hi is not None and n > hi):
                return [f"out of range for {info.builtin}"]
            return []
        if k == "number":
            try:
                float(v)
            except ValueError:
                return ["not a number"]
            return []
        if k == "dateTime":
            return [] if _DATETIME.match(v) else ["not an xs:dateTime"]
        if k == "base64":
            return [] if _BASE64.match(v) else ["not base64"]
        if k == "anyURI":
            return [] if _URI.match(v) else ["whitespace in URI"]
        if k == "QName":
            return [] if v and not any(c.isspace() for c in v) else ["not a QName"]
        if k == "nmtoken":
            return [] if v and not any(c.isspace() for c in v) else ["not an NMTOKEN"]
        if k == "nmtokens":
            return [] if v.split() else ["empty NMTOKENS"]
        return []

    def _facet_errors(self, f: Facets, v: str) -> list[str]:
        errs = []
        n = len(v)
        if f.length is not None and n != f.length:
            errs.append(f"length {n} != {f.length}")
        if f.min_length is not None and n < f.min_length:
            errs.append(f"shorter than {f.min_length}")
        if f.max_length is not None and n > f.max_length:
            errs.append(f"longer than {f.max_length}")
        if f.enumerations and v not in {e.value for e in f.enumerations}:
            errs.append("not one of the enumerated values")
        if f.patterns:
            rxs = [_xsd_regex(p) for p in f.patterns]
            if not any(rx is None or rx.match(v) for rx in rxs):
                errs.append("does not match pattern")
        for attr_name, op, label in (("min_inclusive", lambda a, b: a >= b, ">="),
                                     ("max_inclusive", lambda a, b: a <= b, "<="),
                                     ("min_exclusive", lambda a, b: a > b, ">"),
                                     ("max_exclusive", lambda a, b: a < b, "<")):
            bound = getattr(f, attr_name)
            if bound is not None:
                try:
                    if not op(float(v), float(bound)):
                        errs.append(f"must be {label} {bound}")
                except ValueError:
                    pass
        return errs

    # -- elements
    def element(self, node: ET.Element, decl_type: Any, path: str) -> None:
        t = decl_type
        if isinstance(t, tuple):
            if self.ss.is_builtin(t):
                self.check_simple(self.ss.simple_info(t), node.text or "", path)
                self._no_children(node, path)
                return
            obj = self.ss.lookup_type(t)
            if obj is None:
                return
            t = obj
        if isinstance(t, SimpleType):
            self.check_simple(self.ss.simple_info(t), node.text or "", path)
            self._no_children(node, path)
            self._attrs(node, [], None, path)
            return
        if isinstance(t, ComplexType):
            self.complex(node, t, path)

    def _no_children(self, node: ET.Element, path: str) -> None:
        for c in node:
            if isinstance(c.tag, str):
                self.err(path, f"unexpected child element {_disp(c.tag)} in simple content")
                break

    def complex(self, node: ET.Element, ct: ComplexType, path: str) -> None:
        attrs = self.ss.effective_attributes(ct)
        self._attrs(node, [a for a, _ in attrs], self.ss.effective_any_attribute(ct), path)
        if ct.content == "simple":
            info = self.ss.simple_info(ct)
            self.check_simple(info, node.text or "", path)
            self._no_children(node, path)
            return
        kids = [c for c in node if isinstance(c.tag, str)]
        if ct.content == "empty" and ct.derivation is None:
            if kids:
                self.err(path, "element must be empty")
            return
        if not ct.mixed and (node.text or "").strip() and kids:
            self.err(path, "unexpected text content")
        root = self._root_particle(ct)
        if root is None:
            if kids:
                self.err(path, f"unexpected child element {_disp(kids[0].tag)}")
            return
        ctx = _MatchCtx()
        ends = self._match(root, kids, 0, ctx)
        if len(kids) not in ends:
            self._report_mismatch(kids, ctx, path)
            return
        rows = self.ss.effective_rows(ct)
        for c in kids:
            qn = self.canon(_split(c.tag))
            decl = self._find_decl(rows, qn)
            if decl is None:
                continue  # matched by wildcard: lax
            cpath = f"{path}/{self.ss.qstr(qn)}"
            self._validate_child(c, decl, cpath)

    def _root_particle(self, ct: ComplexType) -> Particle | None:
        parts = [t.particle for t in self.ss.ancestry(ct) if t.particle is not None]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return Particle(kind="sequence", children=parts, target_ns=ct.target_ns)

    def _find_decl(self, rows: list[Row], qn: QName) -> Particle | None:
        for r in rows:
            p = r.particle
            if p.kind == "element" and p.name is not None:
                if self.canon(p.name) == qn:
                    return p
        for r in rows:
            p = r.particle
            if p.kind == "element" and p.name is not None:
                if qn in [self.canon(m) for m in self.ss.substitution_members(p.name)]:
                    return Particle(kind="element", name=qn, is_ref=True, target_ns=p.target_ns)
        return None

    def _validate_child(self, c: ET.Element, decl: Particle, path: str) -> None:
        if decl.is_ref:
            g = self.ss.elements.get(self._unalias(decl.name))
            if g is None:
                return
            t: Any = g.anon_type if g.anon_type is not None else g.type
        else:
            t = decl.anon_type if decl.anon_type is not None else decl.type
        if t is None:
            return
        self.element(c, t, path)

    def _unalias(self, qn: QName | None) -> QName:
        assert qn is not None
        return qn

    # -- attributes
    def _attrs(self, node: ET.Element, uses: list[AttributeUse], anyattr: AnyAttr | None, path: str) -> None:
        present: dict[QName, str] = {_split(k): v for k, v in node.attrib.items()}
        declared = {self.canon(a.name): a for a in uses}
        for qn, a in declared.items():
            if a.required and qn not in present:
                self.err(path, f"missing required attribute {self.ss.qstr(qn)}")
        for qn, val in present.items():
            if qn[0] in (XML_NS, XSI) or qn[0] == "http://www.w3.org/2000/xmlns/":
                continue
            a = declared.get(self.canon(qn))
            if a is None:
                if anyattr is not None and self._wild_ok(anyattr.namespace, qn[0], None):
                    continue
                self.err(path, f"unexpected attribute {self.ss.qstr(qn)}")
                continue
            ty = self.ss.attribute_type(a)
            if ty is not None:
                self.check_simple(self.ss.simple_info(ty), val, f"{path}/@{self.ss.qstr(qn)}")
            if a.fixed is not None and val != a.fixed:
                self.err(path, f"attribute {self.ss.qstr(qn)} must be {a.fixed!r}")

    def _wild_ok(self, token: str, ns: str, tns: str | None) -> bool:
        for tok in token.split() or ["##any"]:
            if tok == "##any":
                return True
            if tok == "##other":
                if tns is None:
                    if ns != "":
                        return True
                    continue
                if ns != tns and ns != "":
                    return True
            elif tok == "##local":
                if ns == "":
                    return True
            elif tok == "##targetNamespace":
                if tns is not None and ns == tns:
                    return True
            elif tok == ns:
                return True
        return False

    # -- content model
    def _kid_qn(self, kid: ET.Element) -> QName:
        return self.canon(_split(kid.tag))

    def _elem_matches(self, p: Particle, kid: ET.Element) -> bool:
        assert p.name is not None
        qn = self._kid_qn(kid)
        want = self.canon(p.name)
        if qn == want:
            return True
        return qn in [self.canon(m) for m in self.ss.substitution_members(p.name)]

    def _match(self, p: Particle, kids: list[ET.Element], i: int, ctx: "_MatchCtx") -> set[int]:
        lo, hi = p.min_occurs, p.max_occurs
        result: set[int] = set()
        cur = {i}
        count = 0
        if lo == 0:
            result |= cur
        while cur and (hi is None or count < hi):
            nxt: set[int] = set()
            for j in cur:
                nxt |= self._once(p, kids, j, ctx)
            count += 1
            if not nxt:
                break
            if count >= lo:
                result |= nxt
            if nxt == cur and count >= lo:
                break
            cur = nxt
            if count > len(kids) + lo + 1:
                break
        return result

    def _once(self, p: Particle, kids: list[ET.Element], j: int, ctx: "_MatchCtx") -> set[int]:
        k = p.kind
        if k == "element":
            ctx.expect(j, self.ss.qstr(p.name))
            if j < len(kids) and self._elem_matches(p, kids[j]):
                ctx.reach(j + 1)
                return {j + 1}
            return set()
        if k == "any":
            ctx.expect(j, f"any({p.namespace})")
            if j < len(kids):
                qn = self._kid_qn(kids[j])
                if self._wild_ok(p.namespace, qn[0], self.alias.get(p.target_ns, p.target_ns)):
                    ctx.reach(j + 1)
                    return {j + 1}
            return set()
        if k == "sequence":
            pos = {j}
            for c in p.children:
                nxt: set[int] = set()
                for q in pos:
                    nxt |= self._match(c, kids, q, ctx)
                pos = nxt
                if not pos:
                    break
            return pos
        if k == "choice":
            out: set[int] = set()
            for c in p.children:
                out |= self._match(c, kids, j, ctx)
            return out
        if k == "all":
            return self._all(p.children, kids, j, ctx)
        if k == "group" and p.group_ref is not None:
            g = self.ss.groups.get(p.group_ref)
            if g is not None and g.particle is not None:
                return self._match(g.particle, kids, j, ctx)
        return set()

    def _all(self, children: list[Particle], kids: list[ET.Element], j: int, ctx: "_MatchCtx") -> set[int]:
        def rec(remaining: tuple[int, ...], pos: int) -> set[int]:
            results: set[int] = set()
            if all(children[r].min_occurs == 0 for r in remaining):
                results.add(pos)
            for r in remaining:
                for np in self._match(children[r], kids, pos, ctx):
                    if np > pos:
                        results |= rec(tuple(x for x in remaining if x != r), np)
            return results

        return rec(tuple(range(len(children))), j)

    def _report_mismatch(self, kids: list[ET.Element], ctx: "_MatchCtx", path: str) -> None:
        far = ctx.furthest
        expected = sorted(ctx.expected.get(far, set()))
        exp = ", ".join(expected[:6]) + ("..." if len(expected) > 6 else "")
        if far < len(kids):
            self.err(path, f"unexpected element {self.ss.qstr(self._kid_qn(kids[far]))} at position {far + 1}"
                           + (f" (expected {exp})" if exp else ""))
        else:
            self.err(path, f"missing required element after position {far}" + (f" (expected {exp})" if exp else ""))


@dataclass
class _MatchCtx:
    furthest: int = 0
    expected: dict[int, set[str]] = field(default_factory=dict)

    def expect(self, j: int, name: str) -> None:
        self.expected.setdefault(j, set()).add(name)

    def reach(self, j: int) -> None:
        if j > self.furthest:
            self.furthest = j


def _short(s: str, n: int = 40) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def _disp(tag: str) -> str:
    ns, local = _split(tag)
    return f"{{{ns}}}{local}" if ns else local


def validate(ss: SchemaSet, xml: str | bytes | ET.Element, *, expect: QName | None = None,
             ns_alias: dict[str, str] | None = None, strict_any: bool = False) -> list[Problem]:
    """Validate one element against its global declaration (or ``expect``).

    ``ns_alias`` maps alternate namespace URIs onto the schema's (e.g. a legacy revision of the
    service namespace); it is applied to both instance and schema names. Elements that fall into
    an ``xs:any`` wildcard are accepted unchecked unless ``strict_any`` demands a declaration.
    """
    if isinstance(xml, ET.Element):
        root = xml
    else:
        root = ET.fromstring(xml)
    v = _Validator(ss, ns_alias or {}, strict_any)
    qn = expect or _split(root.tag)
    canon = v.canon(qn)
    decl = None
    for cand, el in ss.elements.items():
        if v.canon(cand) == canon:
            decl = el
            break
    if decl is None:
        return [Problem(ss.qstr(qn), "no global element declaration")]
    t = decl.anon_type if decl.anon_type is not None else decl.type
    if t is None:
        return []
    v.element(root, t, ss.qstr(canon))
    return v.problems
