import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def backup(source: Path, target: Path):
    """Publish a complete snapshot at a new path without replacing existing data."""
    if target.exists():
        raise FileExistsError(f"Backup destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as directory:
        snapshot = Path(directory) / "snapshot.db"
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            with closing(sqlite3.connect(snapshot)) as dest:
                db.backup(dest)
        os.link(snapshot, target)
