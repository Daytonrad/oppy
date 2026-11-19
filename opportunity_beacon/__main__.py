import argparse
from collections import Counter
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
import sqlite3
from contextlib import closing

from . import database_path, initialize
from .backup import backup
from .models import Registration
from .runtime import LISTING_URL, Outcome, collect, configuration, run_scheduler
from .telegram import Telegram


def show_status(path):
    with closing(sqlite3.connect(path)) as db:
        for outcome, label in (("OK", "Last successful collection"), ("FAILED", "Last failed collection")):
            row = db.execute("""SELECT observed_at, reason FROM collections WHERE outcome=?
                                ORDER BY observed_at DESC, id DESC LIMIT 1""", (outcome,)).fetchone()
            print(f"{label}: {' | '.join(row) if row else 'none'}")
        pending, = db.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()
        print(f"Pending notifications: {pending}")
        print("Telegram: " + ("configured" if Telegram.configured() else "not configured; pending notifications are retained"))
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
    commands = parser.add_subparsers(dest="command")
    for name in ("status", "run", "smoke", "smoke-telegram"):
        commands.add_parser(name)
    commands.add_parser("backup").add_argument("destination", nargs="?")
    args = parser.parse_args()
    path = database_path()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "smoke":
        observation = collect(retry=False)
        for result in (observation.listing, *observation.details):
            if result.status != Outcome.OK:
                stage = "listagem" if observation.failed_url == LISTING_URL else "detalhe"
                parser.exit(1, f"{result.status} · {stage} · {observation.failed_url}: {result.reason}\n")
        labels = {Registration.OPEN: "abertas", Registration.CLOSED: "encerradas",
                  Registration.FULL: "vagas preenchidas", Registration.UNKNOWN: "desconhecida"}
        totals = Counter()
        print(f"Santarém: {len(observation.details)} seleção(ões)")
        for selection, result in zip(observation.listing.value, observation.details):
            counts = Counter(course.registration for course in result.value.courses)
            totals.update(counts)
            print(f"Seleção {selection.notice or selection.url}: " +
                  ", ".join(f"{name}: {counts[state]}" for state, name in labels.items()))
        print(f"Total: {totals.total()} curso(s) · " +
              ", ".join(f"{name}: {totals[state]}" for state, name in labels.items()))
    elif args.command == "smoke-telegram":
        channel = Telegram.configured()
        if not channel:
            parser.exit(1, "Telegram credentials are missing.\n")
        channel.send({"kind": "test"})
        print("Telegram confirmed the test message.")
    elif args.command == "backup":
        default = path.parent / datetime.now(timezone.utc).strftime("backup-%Y%m%dT%H%M%S%fZ.sqlite3")
        target = Path(args.destination or os.getenv("BEACON_BACKUP_PATH") or default)
        backup(path, target)
        print(f"Backup ready: {target}")
    elif args.command == "run":
        interval, threshold = configuration()
        stop = Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        run_scheduler(path, Telegram.configured(), stop, interval=interval, threshold=threshold)
    else:
        version = initialize(path)
        if args.command == "status":
            show_status(path)
        else:
            print(f"Database ready: {path} (schema {version}, WAL)")


if __name__ == "__main__":
    main()
