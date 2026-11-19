"""Source-independent opportunity snapshots and identity comparisons."""

import hashlib
import json
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import StrEnum


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


class Registration(StrEnum):
    UNKNOWN = "unknown"
    OPEN = "open"
    CLOSED = "closed"
    FULL = "full"


@dataclass(frozen=True)
class SelectionKey:
    source: str
    municipality: str
    identifier: str


@dataclass(frozen=True)
class Selection:
    key: SelectionKey
    url: str
    notice: str | None
    registration: Registration
    registration_mode: str | None


@dataclass(frozen=True, order=True)
class Document:
    label: str | None
    url: str


@dataclass(frozen=True)
class Course:
    selection: SelectionKey
    title: str
    alias: str | None
    registration: Registration
    registration_mode: str | None
    venue: str | None
    requirements: str | None
    registration_start: datetime | None
    registration_end: datetime | None
    course_start: date | None
    course_end: date | None

    @property
    def natural_key(self):
        return (self.selection, normalize(self.title).casefold(),
                normalize(self.venue).casefold() if self.venue else None,
                self.course_start, self.course_end)


@dataclass(frozen=True)
class SelectionDetail:
    selection: SelectionKey
    courses: tuple[Course, ...]
    documents: tuple[Document, ...] | None


def _canonical(value):
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return normalize(value)
    return value


def _snapshot(record: Selection | Course | SelectionDetail) -> dict:
    fields = _canonical(asdict(record))
    for collection in ("courses", "documents"):
        if fields.get(collection) is not None:
            fields[collection].sort(key=lambda item: json.dumps(item, sort_keys=True))
    return fields


def content_hash(record: Selection | Course | SelectionDetail) -> str:
    payload = json.dumps(_snapshot(record), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def field_diff(before, after) -> dict:
    """Map changed fields to their canonical (previous, current) values."""
    if type(before) is not type(after):
        raise TypeError("Snapshots must have the same type")
    old, new = _snapshot(before), _snapshot(after)
    return {key: (old[key], value) for key, value in new.items() if old[key] != value}


class MatchStatus(StrEnum):
    MATCHED = "matched"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Match:
    status: MatchStatus
    previous_index: int | None = None


def match_courses(previous: tuple[Course, ...], current: tuple[Course, ...]) -> tuple[Match, ...]:
    """Return one result per current observation. Unmatched history implies no event."""
    matches = [Match(MatchStatus.UNRESOLVED) for course in current]
    available = set(range(len(previous)))
    pending = set(range(len(current)))

    def alias_key(course):
        return (course.selection, course.alias) if course.alias else None

    def natural_key(course):
        key = course.natural_key
        return key if None not in key else None

    for key_of in (alias_key, natural_key):
        old_groups, new_groups = defaultdict(list), defaultdict(list)
        for i in available:
            old_groups[key_of(previous[i])].append(i)
        for j in pending:
            new_groups[key_of(current[j])].append(j)
        for key, new_indices in new_groups.items():
            old_indices = old_groups.get(key, [])
            if key is None or len(new_indices) != 1 or len(old_indices) != 1:
                continue
            i, j = old_indices[0], new_indices[0]
            if previous[i].alias and current[j].alias and previous[i].alias != current[j].alias:
                continue
            matches[j] = Match(MatchStatus.MATCHED, i)
            available.remove(i)
            pending.remove(j)
    return tuple(matches)
