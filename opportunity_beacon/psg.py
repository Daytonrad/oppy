"""Offline parser for public PSG listing and detail HTML supplied by the caller."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import Generic, TypeVar
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import Element, TreeBuilder
from zoneinfo import ZoneInfo

from .models import (Course, Document, Registration, Selection, SelectionDetail,
                     SelectionKey, normalize)

ORIGIN = "https://www.pa.senac.br"
DETAIL_PATH = re.compile(r"/psg/selecao/detalhar/santarem/5/(\d+)/?")
LOCAL_TIME = ZoneInfo("America/Santarem")
T = TypeVar("T")


class Outcome(StrEnum):
    OK = "OK"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ParseResult(Generic[T]):
    status: Outcome
    reason: str
    value: T | None = None


class _Tree(HTMLParser):
    """Build an inert element tree. HTML void elements need no closing tag."""
    void = frozenset("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self):
        super().__init__()
        self.builder = TreeBuilder()
        self.builder.start("root", {})
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.builder.start(tag, dict(attrs))
        if tag in self.void:
            self.builder.end(tag)
        else:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.void:
            return
        if not self.stack or self.stack.pop() != tag:
            raise ValueError(f"Unexpected closing tag: {tag}")
        self.builder.end(tag)

    def handle_data(self, data):
        self.builder.data(data)

    def parse(self, markup):
        self.feed(markup)
        self.close()
        if self.stack:
            raise ValueError(f"Unclosed element: {self.stack[-1]}")
        self.builder.end("root")
        return self.builder.close()


def _text(node: Element) -> str:
    return normalize(" ".join(node.itertext()))


def _class(node, name):
    return name in node.get("class", "").split()


def _one(nodes, description):
    if len(nodes) != 1:
        raise ValueError(f"Expected exactly one {description}, found {len(nodes)}")
    return nodes[0]


def _official_url(href):
    url = urljoin(ORIGIN, href)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "www.pa.senac.br":
        raise ValueError("Unexpected source URL")
    return url


def _selection(row):
    links = row.findall("./a")
    urls = {_official_url(a.get("href", "")) for a in links}
    url = _one(list(urls), "distinct detail URL per selection")
    parts = urlsplit(url)
    match = DETAIL_PATH.fullmatch(parts.path)
    if not match or parts.query or parts.fragment:
        raise ValueError("Detail URL conflicts with Santarém or expected path")
    anchor = links[0]
    notice = re.search(r"Seleção\s+(\d+/\d{4})", _text(anchor))
    labels = [_text(e) for e in anchor.iter("span") if _class(e, "label")]
    status = Registration.OPEN if labels == ["Inscrições Abertas"] else Registration.UNKNOWN
    mode = row.find("./small")
    return Selection(SelectionKey("senac-pa-psg", "Santarém", match[1]), url,
                     notice[1] if notice else None, status, _text(mode) if mode is not None else None)


def parse_listing(markup: str) -> ParseResult[tuple[Selection, ...]]:
    try:
        root = _Tree().parse(markup)
        main = _one(list(root.iter("main")), "main element")
        panels = [e for e in main.iter("div") if _class(e, "panel") and _class(e, "panel-default")]
        if not panels:
            raise ValueError("Municipality panel structure missing")
        targets = []
        for panel in panels:
            headings = [h for d in panel.findall("div") if _class(d, "panel-heading")
                        for h in d.findall("h2") if _class(h, "panel-title")]
            heading = _one(headings, "municipality heading")
            if _text(heading).casefold() == "santarém":
                targets.append(panel)
        if not targets:
            return ParseResult(Outcome.INCONCLUSIVE, "Santarém panel absent")
        panel = _one(targets, "Santarém panel")
        group = _one([e for e in panel.findall("ul") if _class(e, "list-group")], "selection list")
        if not len(group):
            return ParseResult(Outcome.INCONCLUSIVE, "Unverified empty selection list")
        if any(e.tag != "li" or not _class(e, "list-group-item") for e in group):
            raise ValueError("Unexpected selection row")
        records = tuple(_selection(row) for row in group)
        if len({s.key for s in records}) != len(records):
            raise ValueError("Duplicate selection identity")
        return ParseResult(Outcome.OK, "Santarém selection rows parsed", records)
    except ValueError as error:
        return ParseResult(Outcome.FAILED, str(error))


def _course_date(text):
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", text):
        return datetime.strptime(text, "%d/%m/%Y").date()
    return None


def _registration_bound(text):
    if re.fullmatch(r"\d{2}/\d{2}/\d{4} às \d{2}:\d{2}", text):
        return datetime.strptime(text, "%d/%m/%Y às %H:%M").replace(tzinfo=LOCAL_TIME)
    return None


def _course(row, selection):
    body = _one([e for e in row.findall("div") if _class(e, "media-body")], "course body")
    heading = _one([e for e in body.findall("h3") if _class(e, "media-heading")], "course heading")
    title = normalize(heading.text or "")
    if not title:
        raise ValueError("Missing course title")
    mode = heading.find("small")
    segments = []
    for p in body.findall("p"):
        # The source places each labelled field between br elements.
        line = p.text or ""
        for child in p:
            if child.tag == "br":
                segments.append(normalize(line))
                line = ""
            else:
                line += " " + _text(child)
            line += child.tail or ""
        segments.append(normalize(line))

    def labelled(prefix):
        return next((s.removeprefix(prefix).strip() or None for s in segments if s.startswith(prefix)), None)

    venue = labelled("Local do curso:")
    start = end = first = last = None
    registration = labelled("Inscrições de ")
    period = labelled("Período do curso: de ")
    if registration:
        bounds = registration.split(" até ", 1)
        start = _registration_bound(bounds[0])
        if len(bounds) == 2:
            end = _registration_bound(bounds[1])
    if period:
        bounds = period.split(" até ", 1)
        first = _course_date(bounds[0])
        if len(bounds) == 2:
            last = _course_date(bounds[1])
    controls = [a for a in body.iter("a") if _class(a, "btn")]
    if len(controls) > 1:
        raise ValueError("Ambiguous registration control")
    action = controls[0] if controls else Element("a")
    if action.get("data-selecao", selection.key.identifier) != selection.key.identifier or action.get("data-cidade", "5") != "5":
        raise ValueError("Course metadata conflicts with selection")
    status = {"Inscrever neste curso": Registration.OPEN, "Inscrições encerradas": Registration.CLOSED,
              "Vagas preenchidas": Registration.FULL}.get(_text(action), Registration.UNKNOWN)
    return Course(selection.key, title, action.get("data-curso"), status,
                  _text(mode) if mode is not None else None,
                  None if venue == "[venue omitted]" else venue, labelled("Pré-requisito:"),
                  start, end, first, last)


def parse_detail(markup: str, selection: Selection) -> ParseResult[SelectionDetail]:
    try:
        root = _Tree().parse(markup)
        main = _one(list(root.iter("main")), "main element")
        group = _one([e for e in main.iter("ul") if _class(e, "lista-psg-cursos")], "course list")
        if not len(group):
            return ParseResult(Outcome.INCONCLUSIVE, "Unverified empty course list")
        if any(row.tag != "li" for row in group):
            raise ValueError("Unexpected course list structure")
        courses = tuple(_course(row, selection) for row in group)
        docs = []
        for row in main.iter("li"):
            for a in row.findall("a"):
                href = a.get("href", "")
                if urlsplit(href).path.lower().endswith(".pdf"):
                    label = normalize(a.tail or "") or None
                    notice = re.match(r"Edital\s+(\d+/\d{4})", label or "")
                    if notice and selection.notice and notice[1] != selection.notice:
                        raise ValueError("Document notice conflicts with selection")
                    docs.append(Document(label, _official_url(href)))
        return ParseResult(Outcome.OK, "Course rows parsed",
                           SelectionDetail(selection.key, courses, tuple(docs) or None))
    except ValueError as error:
        return ParseResult(Outcome.FAILED, str(error))
