"""Read a consistent notification snapshot from the saved PSG state."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime


def summary_snapshot(db, observed_at):
    counts = dict.fromkeys(("open", "future", "closed", "full", "unknown"), 0)
    eligible = []
    rows = db.execute("""SELECT s.snapshot, c.snapshot FROM courses c
                         JOIN selections s ON s.id=c.selection_id ORDER BY s.id, c.id""")
    for selection_json, course_json in rows:
        selection, course = json.loads(selection_json), json.loads(course_json)
        status = course["registration"]
        start = course.get("registration_start")
        if status == "unknown" and start and datetime.fromisoformat(start) > observed_at:
            status = "future"
        counts[status] += 1
        if status in ("open", "future"):
            eligible.append(dict(course, category=status, notice=selection["notice"], url=selection["url"]))
    eligible.sort(key=lambda c: (c["category"] != "open", c["notice"] or "", c["title"]))
    return {"selections": db.execute("SELECT count(*) FROM selections").fetchone()[0],
            "counts": counts, "eligible": eligible}


def saved_summary(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        last = db.execute("SELECT observed_at FROM collections WHERE outcome='OK' ORDER BY id DESC LIMIT 1").fetchone()
        if not last:
            raise ValueError("Nenhuma coleta válida registrada.")
        return {"kind": "summary", "summary": summary_snapshot(db, datetime.fromisoformat(last[0]))}


def delivery_context(db, event, collection_id):
    """Read presentation context without changing the persisted event or its identity."""
    kind = event["kind"]
    baseline = kind == "baseline"
    if not baseline and (event.get("course_id") is not None or not event.get("selection")):
        return event
    selection_id = json.dumps(event["selection"], ensure_ascii=False, sort_keys=True)
    scope = "" if baseline else " WHERE selection_id=?"
    args = () if baseline else (selection_id,)
    latest_revision, = db.execute("SELECT max(revision) FROM courses" + scope, args).fetchone()
    latest_selection, = db.execute("SELECT max(revision) FROM selections" +
                                   ("" if baseline else " WHERE id=?"), args).fetchone()
    revision = max(collection_id, latest_revision or 0, latest_selection or 0)
    stamp, = db.execute("SELECT observed_at FROM collections WHERE id=?", (revision,)).fetchone()
    if revision > collection_id:
        event = dict(event, detail_time=stamp)
    if baseline:
        return dict(event, summary=summary_snapshot(db, datetime.fromisoformat(stamp)))
    courses = db.execute("SELECT snapshot FROM courses WHERE selection_id=? ORDER BY id", args)
    return dict(event, selection_courses=[json.loads(raw) for raw, in courses])
