"""SQLite initialization for OpportunityBeacon."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

MIGRATIONS = (
    """CREATE TABLE schema_migrations (
           version INTEGER PRIMARY KEY,
           applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
       )""",
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
        for number, statement in enumerate(MIGRATIONS[version:], start=version + 1):
            db.execute(statement)
            db.execute("INSERT INTO schema_migrations(version) VALUES (?)", (number,))
            db.execute(f"PRAGMA user_version={number}")
        return len(MIGRATIONS)
