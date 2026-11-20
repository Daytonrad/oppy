"""SQLite initialization for Oppy."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

MIGRATIONS = (
    ("""CREATE TABLE schema_migrations (
           version INTEGER PRIMARY KEY,
           applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
       )""",),
    (
        """CREATE TABLE collections (
            id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('OK','INCONCLUSIVE','FAILED')),
            reason TEXT NOT NULL)""",
        """CREATE TABLE selections (
            id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, documents TEXT NOT NULL, last_registration TEXT,
            revision INTEGER NOT NULL REFERENCES collections(id))""",
        """CREATE TABLE courses (
            id INTEGER PRIMARY KEY, selection_id TEXT NOT NULL REFERENCES selections(id),
            snapshot TEXT NOT NULL, last_alias TEXT, last_registration TEXT,
            revision INTEGER NOT NULL REFERENCES collections(id))""",
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY, collection_id INTEGER NOT NULL REFERENCES collections(id),
            dedup_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, payload TEXT NOT NULL)""",
        """CREATE TABLE outbox (
            event_id INTEGER PRIMARY KEY REFERENCES events(id),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent')),
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, last_failure_at TEXT,
            sent_at TEXT)""",
    ),
    ("ALTER TABLE collections ADD COLUMN retry_not_before REAL",),
)


def database_path() -> Path:
    return Path(os.environ.get("BEACON_DB_PATH", "data/beacon.sqlite3"))


def initialize(path: Path) -> int:
    """Apply pending migrations atomically and return the schema version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode != "wal":
            raise RuntimeError(f"SQLite could not enable WAL (got {mode})")

        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= version <= len(MIGRATIONS):
            raise ValueError(f"Unsupported database schema version: {version}")
        for number, statements in enumerate(MIGRATIONS[version:], start=version + 1):
            for statement in statements:
                db.execute(statement)
            db.execute("INSERT INTO schema_migrations(version) VALUES (?)", (number,))
            db.execute(f"PRAGMA user_version={number}")
        return len(MIGRATIONS)
