import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from opportunity_beacon import initialize
from opportunity_beacon.core import record_collection
from opportunity_beacon.models import Document, Registration
from opportunity_beacon.outbox import MemoryChannel, deliver_pending
from opportunity_beacon.psg import Outcome, ParseResult, parse_detail, parse_listing

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def ok(value):
    return ParseResult(Outcome.OK, "Fixture collection", value)


@pytest.fixture
def samples():
    listing = parse_listing((FIXTURES / "psg-listing.html").read_text())
    return [(s, parse_detail((FIXTURES / f"psg-detail-{s.notice[:3]}.html").read_text(), s).value)
            for s in listing.value]


def collect(path, pairs):
    return record_collection(path, ok(tuple(s for s, _ in pairs)),
                             [ok(d) for _, d in pairs], observed_at=NOW)


def query(path, sql):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(sql).fetchall()


def kinds(path):
    return [kind for kind, in query(path, "SELECT kind FROM events ORDER BY id")]


def test_baseline_and_repeated_fixtures(tmp_path, samples):
    path = tmp_path / "state.db"
    for _ in range(10):
        assert collect(path, samples) == Outcome.OK
    assert kinds(path) == ["baseline"]
    payload, = query(path, "SELECT payload FROM events")[0]
    assert json.loads(payload)["after"] == {
        "courses": 4, "selections": 2, "open_courses": ["Criando Meu Primeiro Game"]}
    assert query(path, "SELECT status, attempts FROM outbox") == [("pending", 0)]


def test_lifecycle_and_alias_disappears(tmp_path, samples):
    path = tmp_path / "state.db"
    selection, detail = samples[0]
    course, = detail.courses
    assert collect(path, [samples[1]]) == Outcome.OK

    def observe(registration, alias):
        changed = replace(course, registration=registration, alias=alias)
        return collect(path, [(selection, replace(detail, courses=(changed,)))])

    assert observe(Registration.UNKNOWN, None) == Outcome.OK
    assert observe(Registration.OPEN, "course-7") == Outcome.OK
    existing = query(path, "SELECT id FROM courses WHERE last_alias='course-7'")
    before = len(kinds(path))
    for _ in range(10):
        assert observe(Registration.CLOSED, None) == Outcome.OK
    assert kinds(path)[before:] == ["registration_closed"]
    assert query(path, "SELECT id FROM courses WHERE last_alias='course-7'") == existing
    for state in [Registration.OPEN, Registration.CLOSED, Registration.OPEN, Registration.FULL]:
        observe(state, "course-7")
    result = kinds(path)
    for kind in ["new_course", "new_selection", "registration_announced", "registration_opened", "places_filled"]:
        assert result.count(kind) == 1
    assert result.count("registration_reopened") == 2
    assert result.count("registration_closed") == 2
    assert query(path, "SELECT count(*) FROM courses") == [(4,)]


def test_changes_within_known_selection(tmp_path, samples):
    path = tmp_path / "state.db"
    selection, detail = samples[0]
    collect(path, samples)
    original, = detail.courses
    added = replace(original, title="Additional course")
    changed = replace(original, registration_end=NOW)
    amend = Document("Retificativo II", "https://example.invalid/correction.pdf")
    extra = Document("Edital adicional", "https://example.invalid/notice.pdf")
    observed = replace(detail, courses=(added, changed), documents=(amend, extra))
    for _ in range(2):
        collect(path, [(selection, observed)])
    assert sorted(kinds(path)[1:]) == sorted(["new_course", "registration_announced", "dates_changed",
                                             "amendment", "new_document"])
    assert query(path, "SELECT COUNT(*) FROM selections") == [(2,)]


def test_invalid_or_partial_collection_preserves_state(tmp_path, samples):
    path = tmp_path / "state.db"
    collect(path, samples)
    tables = ["selections", "courses"]
    previous = [query(path, f"SELECT * FROM {table}") for table in tables]
    for state in (Outcome.INCONCLUSIVE, Outcome.FAILED):
        assert record_collection(path, ParseResult(state, "Listing unavailable")) == state
        assert record_collection(path, ok(tuple(s for s, _ in samples)),
                                 [ok(samples[0][1]), ParseResult(state, "Detail unavailable")]) == state
    assert previous == [query(path, f"SELECT * FROM {table}") for table in tables]
    assert kinds(path) == ["baseline", "monitoring_failure"]
    assert query(path, "SELECT count(*) FROM collections") == [(5,)]


def test_atomic_rollback_on_outbox_failure(tmp_path, samples):
    path = tmp_path / "state.db"
    initialize(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TRIGGER reject BEFORE INSERT ON outbox BEGIN
                      SELECT RAISE(ABORT,'test interruption'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test interruption"):
        collect(path, samples)
    for table in ["collections", "selections", "courses", "events", "outbox"]:
        assert query(path, f"SELECT COUNT(*) FROM {table}") == [(0,)]


def test_delivery_crash_and_restart(tmp_path, samples):
    path = tmp_path / "state.db"
    collect(path, samples)
    env = {**os.environ, "BEACON_DB_PATH": str(path)}
    code = """from opportunity_beacon import database_path
from opportunity_beacon.outbox import deliver_pending, MemoryChannel
import os
class Interrupted(MemoryChannel):
    def send(self, message):
        super().send(message)
        os._exit(23)
deliver_pending(database_path(), Interrupted())"""
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 23
    assert query(path, "SELECT status, attempts FROM outbox") == [("pending", 1)]
    retry = """from opportunity_beacon import database_path
from opportunity_beacon.outbox import deliver_pending, MemoryChannel
channel = MemoryChannel()
deliver_pending(database_path(), channel)
print([m['kind'] for m in channel.messages])"""
    run = subprocess.run([sys.executable, "-c", retry], env=env, capture_output=True, text=True, check=True)
    assert run.stdout.strip() == "['baseline']"
    assert query(path, "SELECT status, attempts FROM outbox") == [("sent", 2)]
    assert deliver_pending(path, MemoryChannel()) == 0


@pytest.mark.parametrize("raises", [False, True])
def test_failed_delivery_and_status(tmp_path, samples, raises):
    path = tmp_path / "state.db"
    collect(path, samples)
    record_collection(path, ParseResult(Outcome.FAILED, "Broken listing"))

    class Broken:
        def send(self, message):
            if raises:
                raise OSError("offline failure")
            return False

    assert deliver_pending(path, Broken()) == 0
    state, attempts, error = query(path, "SELECT status, attempts, last_error FROM outbox")[0]
    assert (state, attempts) == ("pending", 1)
    assert error == ("offline failure" if raises else "Channel did not confirm delivery")
    run = subprocess.run([sys.executable, "-m", "opportunity_beacon", "status"],
                         env={**os.environ, "BEACON_DB_PATH": str(path)}, check=True,
                         capture_output=True, text=True)
    for expected in ("Last successful collection: 2026-10-01", "Broken listing",
                     "Pending notifications: 1", error):
        assert expected in run.stdout
    assert deliver_pending(path, MemoryChannel()) == 1


def test_ambiguous_change_rolls_back_earlier_selection(tmp_path, samples):
    path = tmp_path / "state.db"
    collect(path, samples)
    selection, detail = samples[0]
    other, multiple = samples[1]
    before = query(path, "SELECT snapshot FROM courses ORDER BY id")
    revised = replace(detail.courses[0], course_end=NOW.date())
    conflict = replace(multiple.courses[0], title="Possible rename")
    outcome = collect(path, [(selection, replace(detail, courses=(revised,))),
                             (other, replace(multiple, courses=(conflict,)))])
    assert outcome == Outcome.INCONCLUSIVE
    assert query(path, "SELECT snapshot FROM courses ORDER BY id") == before
    assert kinds(path) == ["baseline"]


def test_selection_status_is_separate_from_course_status(tmp_path, samples):
    path = tmp_path / "state.db"
    selection, detail = samples[0]
    collect(path, [(selection, detail)])
    collect(path, [(replace(selection, registration=Registration.CLOSED), detail)])
    collect(path, [(selection, detail)])
    assert kinds(path) == ["baseline", "registration_closed", "registration_reopened"]
    assert all(json.loads(raw)["course_id"] is None for raw, in query(path, "SELECT payload FROM events"))


def test_upgrade_from_schema_one(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)")
        db.execute("INSERT INTO schema_migrations VALUES (1, 'original migration')")
        db.execute("PRAGMA user_version=1")
    assert initialize(path) == initialize(path) == 3
    assert query(path, "SELECT applied_at FROM schema_migrations WHERE version=1") == [("original migration",)]
    assert query(path, "PRAGMA foreign_key_check") == []


def test_alias_loss_at_closure_uses_unique_title_and_venue(tmp_path, samples):
    path = tmp_path / "alias-loss.db"
    selection, detail = samples[0]
    original = detail.courses[0]
    opened = replace(original, alias="test-course-id", registration=Registration.OPEN)
    closed = replace(opened, alias=None, registration=Registration.CLOSED)
    collect(path, [(selection, replace(detail, courses=(opened,)))])
    [(original_id,)] = query(path, "SELECT id FROM courses")
    assert collect(path, [(selection, replace(detail, courses=(closed,)))]) == Outcome.OK
    assert kinds(path) == ["baseline", "registration_closed"]
    assert query(path, "SELECT id, last_alias FROM courses") == [(original_id, "test-course-id")]
    [(payload,)] = query(path, "SELECT payload FROM events WHERE kind='registration_closed'")
    assert json.loads(payload)["course_id"] == original_id
