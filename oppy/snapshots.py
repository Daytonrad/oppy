"""Owner-only diagnostics for HTML rejected by the parser."""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .http_client import MAX_PAGE_BYTES


SNAPSHOT_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z-(?:listing|detail)-[0-9a-f]{32}")


def snapshot_files(directory):
    pairs = []
    for folder in directory.glob("*"):
        if not SNAPSHOT_NAME.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
            continue
        html = folder / f"{folder.name}.html"
        meta = html.with_suffix(".json")
        if all(not path.is_symlink() and path.is_file() for path in (html, meta)):
            pairs.append(html)
    return sorted(pairs, reverse=True)


def save_snapshot(directory, kind, url, body, result):
    if directory is None or len(body) > MAX_PAGE_BYTES:
        return
    try:
        stamp = datetime.now(timezone.utc)
        name = f"{stamp:%Y%m%dT%H%M%S%fZ}-{kind}-{uuid4().hex}"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = {"url": url, "timestamp": stamp.isoformat(), "http_status": 200,
                    "result": result.status.value, "reason": result.reason}
        # Publish the pair together, then prune only completed snapshots.
        with tempfile.TemporaryDirectory(dir=directory, prefix=".pending-") as tmp:
            pair = Path(tmp) / name
            pair.mkdir(mode=0o700)
            for extension, content in (("html", body), ("json", json.dumps(metadata).encode())):
                fd = os.open(pair / f"{name}.{extension}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
            pair.rename(directory / name)
        for html in snapshot_files(directory)[10:]:
            html.with_suffix(".json").unlink(missing_ok=True)
            html.unlink(missing_ok=True)
            html.parent.rmdir()
    except OSError as error:
        logging.getLogger(__name__).warning("Could not maintain failed HTML snapshot: %s", error)
