"""Persistent delivery to a caller-supplied notification channel."""

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


from .summary import delivery_context


class DeliveryUnavailable(RuntimeError):
    """Pause this batch when the provider is unavailable."""


class Channel(Protocol):
    def send(self, notification: dict) -> bool:
        """Return True only after accepting the notification."""
        ...


class MemoryChannel:
    def __init__(self):
        self.messages = []

    def send(self, notification: dict) -> bool:
        self.messages.append(notification)
        return True


def deliver_pending(path: Path, channel: Channel, *, should_stop=lambda: False) -> int:
    """Attempt each pending row once per call, outside the collection transaction."""
    sent = 0
    with closing(sqlite3.connect(path)) as db:
        pending = db.execute("""SELECT event_id, payload, collection_id FROM outbox JOIN events ON id=event_id
                                WHERE status='pending' ORDER BY event_id""").fetchall()
        for event_id, payload, collection_id in pending:
            if should_stop():
                break
            with db:
                claimed = db.execute("""UPDATE outbox SET attempts=attempts+1
                                        WHERE event_id=? AND status='pending'""", (event_id,))
            if not claimed.rowcount:
                continue
            try:
                with db:
                    db.execute("BEGIN")
                    notification = delivery_context(db, dict(json.loads(payload), event_id=event_id), collection_id)
                confirmed = channel.send(notification)
                if confirmed is not True:
                    raise RuntimeError("Channel did not confirm delivery")
            except Exception as error:
                with db:
                    db.execute("""UPDATE outbox SET last_error=?, last_failure_at=?
                                  WHERE event_id=? AND status='pending'""",
                               (str(error) or type(error).__name__, datetime.now(timezone.utc).isoformat(), event_id))
                logging.getLogger(__name__).warning("Delivery failed for event %s", event_id)
                if isinstance(error, DeliveryUnavailable):
                    break
                continue
            with db:
                sent += db.execute("""UPDATE outbox SET status='sent', sent_at=?
                                      WHERE event_id=? AND status='pending'""",
                                   (datetime.now(timezone.utc).isoformat(), event_id)).rowcount
    return sent
