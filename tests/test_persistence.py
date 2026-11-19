import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory


class PersistenceTest(unittest.TestCase):
    def test_database_survives_second_start(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "test.db"
            env = {**os.environ, "BEACON_DB_PATH": str(path)}
            command = [sys.executable, "-m", "opportunity_beacon"]
            subprocess.run(command, env=env, check=True)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE probe (value TEXT)")
                db.execute("INSERT INTO probe VALUES ('first run')")

            subprocess.run(command, env=env, check=True)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT value FROM probe").fetchall(), [("first run",)])
                self.assertEqual(db.execute("SELECT version FROM schema_migrations").fetchall(), [(1,), (2,), (3,)])
                self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (3,))
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone(), ("wal",))
