import json
import os
import stat
import subprocess
import sys
from datetime import datetime
from email.message import Message
from pathlib import Path

import pytest

from oppy import runtime
from oppy.psg import Outcome
from oppy.snapshots import snapshot_files


@pytest.mark.parametrize("kind", ["listing", "detail"])
def test_raw_snapshot_metadata_and_status(tmp_path, monkeypatch, kind):
    folder = tmp_path / "failed-snapshots"
    fixture = Path(__file__).parent / "fixtures/psg-listing.html"
    raw = b"<main>p\xe1gina alterada</main>\r\n"
    urls = []
    def transport(req, limit):
        urls.append(req.full_url)
        headers = Message()
        headers["Content-Type"] = "text/html; charset=iso-8859-1"
        return (fixture.read_text().encode("iso-8859-1") if kind == "detail" and len(urls) == 1 else raw), headers
    monkeypatch.setattr(runtime, "request", transport)
    obs = runtime.collect(snapshot_dir=folder)
    result = obs.details[-1] if kind == "detail" else obs.listing
    assert result.status == Outcome.FAILED
    html, = snapshot_files(folder)
    meta = html.with_suffix(".json")
    assert html.read_bytes() == raw and f"-{kind}-" in html.name
    data = json.loads(meta.read_text())
    assert data["url"] == urls[-1] == obs.failed_url
    assert data["http_status"] == 200 and data["result"] == "FAILED"
    assert data["reason"] == result.reason
    assert datetime.fromisoformat(data["timestamp"]).utcoffset().total_seconds() == 0
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in (html, meta))
    assert stat.S_IMODE(html.parent.stat().st_mode) == 0o700
    run = subprocess.run([sys.executable, "-m", "oppy", "status"], capture_output=True,
                         text=True, check=True, env={**os.environ, "BEACON_DB_PATH": str(tmp_path / "db")})
    assert "Failed HTML snapshots: 1" in run.stdout and str(html) in run.stdout


def test_retention_and_write_failure(tmp_path, caplog):
    folder = tmp_path / "failed-snapshots"
    fixture = Path(__file__).parent / "fixtures/psg-listing.html"
    markup = fixture.read_text().replace("Santarém", "Belém")
    for n in range(12):
        result = runtime.collect(lambda _: markup + f"<!--{n}-->", snapshot_dir=folder)
        assert result.listing.status == Outcome.INCONCLUSIVE
    files = snapshot_files(folder)
    assert len(files) == len(list(folder.rglob("*.json"))) == 10
    assert [p.read_text().split("<!--")[-1] for p in files] == [f"{n}-->" for n in range(11, 1, -1)]
    blocked = tmp_path / "regular-file"
    blocked.touch()
    failed_write = runtime.collect(lambda _: markup, snapshot_dir=blocked)
    assert failed_write.listing == result.listing
    assert "Could not maintain failed HTML snapshot" in caplog.text


def test_only_completed_oppy_pairs_participate_in_retention(tmp_path):
    folder = tmp_path / "failed-snapshots"
    preserved = []
    for name in ("2026-manual", "20260101T120000000000Z-detail-" + "a" * 32):
        pair = folder / name
        pair.mkdir(parents=True)
        html = pair / f"{name}.html"
        html.write_text("keep")
        preserved.append(html)
    assert snapshot_files(folder) == []
    for _ in range(11):
        runtime.collect(lambda _: "<main></main>", snapshot_dir=folder)
    assert len(snapshot_files(folder)) == 10
    assert all(html.read_text() == "keep" for html in preserved)
