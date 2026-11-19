"""Apply a complete offline collection as a single SQLite transaction."""

import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path

from . import initialize
from .models import Course, Registration, SelectionKey, content_hash, field_diff, match_courses
from .psg import Outcome, ParseResult


def encode(value):
    def temporal(item):
        if isinstance(item, date):
            return item.isoformat()
        raise TypeError(f"Cannot encode {type(item).__name__}")

    return json.dumps(value, default=temporal, ensure_ascii=False, sort_keys=True)


def read_course(raw):
    data = json.loads(raw)
    data["selection"] = SelectionKey(**data["selection"])
    data["registration"] = Registration(data["registration"])
    for name in ("course_start", "course_end"):
        if data[name]:
            data[name] = date.fromisoformat(data[name])
    for name in ("registration_start", "registration_end"):
        if data[name]:
            data[name] = datetime.fromisoformat(data[name])
    return Course(**data)


class UncertainIdentity(ValueError):
    pass


def align(rows, courses):
    old = [read_course(row["snapshot"]) for row in rows]
    remembered = tuple(replace(c, alias=r["last_alias"]) for c, r in zip(old, rows))
    matches = match_courses(remembered, courses)
    aligned = [m.previous_index for m in matches]
    unused = set(range(len(old))) - {i for i in aligned if i is not None}

    # Identical observations need no identity decision or write, even for redacted venues.
    remaining = [j for j, i in enumerate(aligned) if i is None]
    old_groups, new_groups = defaultdict(list), defaultdict(list)
    for i in sorted(unused):
        old_groups[content_hash(old[i])].append(i)
    for j in remaining:
        new_groups[content_hash(courses[j])].append(j)
    for digest, indices in new_groups.items():
        equal = old_groups[digest]
        if len(equal) == len(indices):
            for j, i in zip(indices, equal):
                aligned[j] = i
                unused.remove(i)

    # An unmatched old row could be a rename or a changed class. Defer the
    # collection unless a distinct known alias rules out every such pairing.
    for j, i in enumerate(aligned):
        if i is None and any(not courses[j].alias or not remembered[k].alias
                             or courses[j].alias == remembered[k].alias for k in unused):
            raise UncertainIdentity(f"Unresolved course identity: {courses[j].title}")
    return old, aligned


def record_collection(path: Path, listing: ParseResult, details=(), *, observed_at=None,
                      failure_threshold=3, retry_not_before=None):
    """Consume a listing and one detail ParseResult per listed selection."""
    now = observed_at or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    stamp = now.astimezone(timezone.utc).isoformat()
    details = tuple(details)
    outcome, reason = listing.status, listing.reason
    if outcome == Outcome.OK:
        problem = next((r for state in (Outcome.FAILED, Outcome.INCONCLUSIVE)
                        for r in details if r.status == state), None)
        if problem:
            outcome, reason = problem.status, problem.reason
        elif not listing.value or any(not r.value or not r.value.courses for r in details):
            outcome, reason = Outcome.INCONCLUSIVE, "Incomplete collection"
        else:
            keys = {s.key for s in listing.value}
            by_key = {r.value.selection: r.value for r in details}
            if (keys != by_key.keys() or len(details) != len(keys) or len(keys) != len(listing.value)
                    or any(c.selection != d.selection for d in by_key.values() for c in d.courses)):
                outcome, reason = Outcome.FAILED, "Selection and detail identities disagree"

    initialize(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        batch = db.execute("INSERT INTO collections(observed_at,outcome,reason,retry_not_before) VALUES (?,?,?,?)",
                           (stamp, outcome, reason, retry_not_before)).lastrowid
        if outcome != Outcome.OK:
            _health(db, batch, outcome, reason, failure_threshold)
            return outcome
        baseline = not db.execute("SELECT 1 FROM selections LIMIT 1").fetchone()
        db.execute("SAVEPOINT snapshots")
        try:
            for selection in listing.value:
                _save_selection(db, batch, selection, by_key[selection.key], now, baseline)
        except UncertainIdentity as error:
            db.execute("ROLLBACK TO snapshots")
            db.execute("UPDATE collections SET outcome='INCONCLUSIVE', reason=? WHERE id=?",
                       (str(error), batch))
            _health(db, batch, Outcome.INCONCLUSIVE, str(error), failure_threshold)
            return Outcome.INCONCLUSIVE
        if baseline:
            courses = [c for d in by_key.values() for c in d.courses]
            summary = {"selections": len(by_key), "courses": len(courses),
                       "open_courses": [c.title for c in courses if c.registration == Registration.OPEN]}
            _event(db, batch, listing.value[0].key.source, None, None, "baseline", 0, None, summary, {})
        _health(db, batch, Outcome.OK, reason, failure_threshold)
        return Outcome.OK


def _health(db, batch, outcome, reason, threshold):
    previous = db.execute("""SELECT kind FROM events WHERE kind IN
                             ('monitoring_failure','monitoring_recovered') ORDER BY id DESC LIMIT 1""").fetchone()
    alerted = previous is not None and previous[0] == "monitoring_failure"
    if outcome == Outcome.OK:
        if alerted:
            _event(db, batch, "senac-pa-psg", None, None, "monitoring_recovered", batch, None, None, {})
    elif not alerted:
        count, = db.execute("""SELECT count(*) FROM collections WHERE id >
                            coalesce((SELECT max(id) FROM collections WHERE outcome='OK'),0)""").fetchone()
        if count >= threshold:
            _event(db, batch, "senac-pa-psg", None, None, "monitoring_failure", batch, None,
                   {"consecutive": count, "reason": reason}, {})


def _event(db, batch, source, selection, course, kind, revision, before, after, context):
    identity = [source, selection, course, kind, revision, before, after]
    key = hashlib.sha256(encode(identity).encode()).hexdigest()
    payload = dict(context, source=source, selection=selection, course_id=course,
                   kind=kind, before=before, after=after)
    event = db.execute("INSERT INTO events(collection_id,dedup_key,kind,payload) VALUES (?,?,?,?)",
                       (batch, key, kind, encode(payload))).lastrowid
    db.execute("INSERT INTO outbox(event_id) VALUES (?)", (event,))


def _save_selection(db, batch, selection, detail, now, baseline):
    key = asdict(selection.key)
    sid = encode(key)
    previous = db.execute("SELECT * FROM selections WHERE id=?", (sid,)).fetchone()
    documents = json.loads(previous["documents"]) if previous else {}
    snapshot = encode(asdict(selection))
    context = {"url": selection.url, "notice": selection.notice}

    def emit(course, kind, revision, before, after, **extra):
        if not baseline:
            _event(db, batch, selection.key.source, key, course, kind, revision,
                   before, after, context | extra)

    revision = previous["revision"] if previous else 0
    previous_status = previous["last_registration"] if previous else None
    known_status = previous_status if selection.registration == Registration.UNKNOWN else selection.registration
    if previous:
        kind = _status_event(previous_status, selection.registration)
        if kind:
            emit(None, kind, revision, previous_status, selection.registration)
    else:
        emit(None, "new_selection", 0, None, asdict(selection))
    for doc in detail.documents or ():
        if doc.url not in documents or (doc.label != documents[doc.url] and
                                       (doc.label or "").casefold().startswith("retific")):
            kind = "amendment" if (doc.label or "").casefold().startswith("retific") else "new_document"
            emit(None, kind, revision, documents.get(doc.url), asdict(doc))
        documents[doc.url] = doc.label
    docs = encode(documents)
    if not previous or previous["snapshot"] != snapshot or previous["documents"] != docs:
        db.execute("""INSERT INTO selections(id,snapshot,documents,last_registration,revision) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                      snapshot=excluded.snapshot, documents=excluded.documents, last_registration=excluded.last_registration,
                      revision=excluded.revision""",
                   (sid, snapshot, docs, known_status, batch))
    rows = db.execute("SELECT * FROM courses WHERE selection_id=? ORDER BY id", (sid,)).fetchall()
    old, aligned = align(rows, detail.courses)
    for course, index in zip(detail.courses, aligned):
        row = rows[index] if index is not None else None
        before = old[index] if row else None
        changes = field_diff(before, course) if before else {}
        if row and not changes:
            continue
        raw = encode(asdict(course))
        prior_status = row["last_registration"] if row else None
        known_status = prior_status if course.registration == Registration.UNKNOWN else course.registration
        if row:
            cid = row["id"]
            db.execute("""UPDATE courses SET snapshot=?, last_alias=?, last_registration=?,
                          revision=? WHERE id=?""", (raw, course.alias or row["last_alias"], known_status, batch, cid))
        else:
            cid = db.execute("""INSERT INTO courses(selection_id,snapshot,last_alias,last_registration,revision)
                                VALUES (?,?,?,?,?)""", (sid, raw, course.alias, known_status, batch)).lastrowid
        for kind, previous_value, current_value in _course_events(course, before, changes, prior_status, now):
            emit(cid, kind, row["revision"] if row else 0, previous_value, current_value, title=course.title,
                 course_state=asdict(course))


def _course_events(course, before, changes, prior_status, now):
    if before is None:
        yield "new_course", None, asdict(course)
    if (course.registration == Registration.UNKNOWN and course.registration_start
            and course.registration_start > now and (not before or "registration_start" in changes)):
        yield "registration_announced", before.registration_start if before else None, course.registration_start
    kind = _status_event(prior_status, course.registration)
    if kind:
        yield kind, prior_status, course.registration
    dates = {k: v for k, v in changes.items()
             if k in ("registration_start", "registration_end", "course_start", "course_end")}
    if dates:
        yield "dates_changed", {k: v[0] for k, v in dates.items()}, {k: v[1] for k, v in dates.items()}


def _status_event(previous, current):
    if current == Registration.UNKNOWN or current == previous:
        return None
    if current == Registration.OPEN and previous in (Registration.CLOSED, Registration.FULL):
        return "registration_reopened"
    return {Registration.OPEN: "registration_opened", Registration.CLOSED: "registration_closed",
            Registration.FULL: "places_filled"}[current]
