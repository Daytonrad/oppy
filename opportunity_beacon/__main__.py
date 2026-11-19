import argparse
import sqlite3
from contextlib import closing

from . import database_path, initialize


def show_status(path):
    with closing(sqlite3.connect(path)) as db:
        for outcome, label in (("OK", "Last successful collection"), ("FAILED", "Last failed collection")):
            row = db.execute("""SELECT observed_at, reason FROM collections WHERE outcome=?
                                ORDER BY observed_at DESC, id DESC LIMIT 1""", (outcome,)).fetchone()
            print(f"{label}: {' | '.join(row) if row else 'none'}")
        pending, = db.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()
        print(f"Pending notifications: {pending}")
        errors = db.execute("""SELECT observed_at AS at, outcome AS category, reason FROM collections
                               WHERE outcome != 'OK'
                               UNION ALL SELECT last_failure_at, 'delivery', last_error FROM outbox
                               WHERE last_failure_at IS NOT NULL ORDER BY at DESC LIMIT 5""").fetchall()
        print("Recent errors:")
        for row in errors:
            print(" | ".join(row))
        if not errors:
            print("none")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=["status"])
    args = parser.parse_args()
    path = database_path()
    version = initialize(path)
    if args.command == "status":
        show_status(path)
    else:
        print(f"Database ready: {path} (schema {version}, WAL)")


if __name__ == "__main__":
    main()
