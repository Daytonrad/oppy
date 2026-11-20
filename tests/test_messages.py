import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from oppy.core import record_collection
from oppy.outbox import DeliveryUnavailable, MemoryChannel, deliver_pending
from oppy.psg import parse_detail, parse_listing
from oppy.summary import saved_summary
from oppy.telegram import Telegram, blocks, message, pages

FIXTURES = Path(__file__).parent / "fixtures"


def seed(path):
    listing = parse_listing((FIXTURES / "psg-listing.html").read_text())
    details = []
    for selection in listing.value:
        number = selection.notice.split("/")[0]
        details.append(parse_detail((FIXTURES / f"psg-detail-{number}.html").read_text(), selection))
    record_collection(path, listing, details, observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc))


def dump(path):
    with closing(sqlite3.connect(path)) as db:
        return list(db.iterdump())


def test_saved_summary_and_initial_event_match(tmp_path):
    path = tmp_path / "db"
    seed(path)
    event = saved_summary(path)
    assert event["summary"]["counts"] == {"open": 1, "future": 1, "closed": 1, "full": 1, "unknown": 0}
    channel = MemoryChannel()
    assert deliver_pending(path, channel) == 1
    baseline, = channel.messages
    assert blocks(baseline)[1:] == blocks(event)[1:]
    opened, future = blocks(event)[1:3]
    assert "Criando Meu Primeiro Game" in opened and "Seleção: 126/2026" in opened
    assert "Local:" not in opened and "[venue omitted]" not in opened
    assert "Situação: Inscrições futuras" in future and "Local: Casa Azul" in future
    assert "04/11/2026 às 09:00" in future and "Fim do curso: 16/12/2026" in future
    assert "/santarem/5/948" in future
    assert "Defesa de Redes" not in message(event) and "Assistente de Recursos Humanos" not in message(event)
    before = dump(path)
    result = subprocess.run([sys.executable, "-m", "oppy", "summary"],
                            env={**os.environ, "BEACON_DB_PATH": str(path)}, text=True,
                            capture_output=True, check=True)
    assert result.stdout.rstrip() == message(event)
    assert dump(path) == before


def test_numbered_pages_keep_whole_blocks_and_order(tmp_path):
    path = tmp_path / "db"
    seed(path)
    event = saved_summary(path)
    summary = event["summary"]
    original = summary["eligible"]
    summary["eligible"] = [dict(original[i % 2], title=f"Turma {i:03} 🗓️ " + "á" * 47) for i in range(140)]
    summary["counts"].update(open=70, future=70)
    summary_parts = pages(event)
    assert len(summary_parts) >= 10
    bodies = []
    for i, part in enumerate(summary_parts, 1):
        prefix, body = part.split("\n", 1)
        assert prefix == f"{i}/{len(summary_parts)}"
        assert len(part.encode("utf-16-le")) <= 8192
        bodies.extend(body.split("\n\n"))
    assert bodies == blocks(event)
    sent = []
    def transport(req, limit):
        sent.append(json.loads(req.data)["text"])
        return b'{"ok":true,"result":{"message_id":1}}', {}
    assert Telegram("123:fake", "fixture", transport=transport).send(event)
    assert sent == summary_parts
    summary["eligible"][0]["title"] = "x" * 4096
    with pytest.raises(DeliveryUnavailable, match="bloco"):
        pages(event)


def test_date_change_and_document_blocks(tmp_path):
    path = tmp_path / "db"
    seed(path)
    state = saved_summary(path)["summary"]["eligible"][0]
    event = dict(kind="dates_changed", course_state=state, notice=state["notice"], url=state["url"],
                 before={"registration_end": "2026-11-30T18:00:00-03:00"},
                 after={"registration_end": "2026-12-01T18:00:00-03:00"})
    text = message(event)
    assert "30/11/2026 às 18:00 → 01/12/2026 às 18:00" in text
    assert "Início das inscrições" not in text and "Fim do curso" not in text
    doc = dict(kind="amendment", after={"label": "Retificativo I", "url": "https://example.invalid/doc.pdf"},
               notice=state["notice"], url=state["url"], selection_courses=[state])
    assert blocks(doc)[1] == blocks(dict(kind="new_course", after=state, notice=state["notice"], url=state["url"]))[1]
    assert "Retificativo I" in blocks(doc)[0]


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    result = subprocess.run([sys.executable, "-m", "oppy", "summary"],
                            env={**os.environ, "BEACON_DB_PATH": str(path)}, capture_output=True)
    assert result.returncode and not path.exists()


def test_delayed_baseline_labels_updated_snapshot(tmp_path):
    path = tmp_path / "db"
    seed(path)
    listing = parse_listing((FIXTURES / "psg-listing.html").read_text())
    details = []
    for selection in listing.value:
        markup = (FIXTURES / f"psg-detail-{selection.notice[:3]}.html").read_text()
        if selection.notice == "152/2026":
            markup = markup.replace("16/12/2026", "18/12/2026")
        details.append(parse_detail(markup, selection))
    record_collection(path, listing, details, observed_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
    channel = MemoryChannel()
    deliver_pending(path, channel)
    baseline = message(channel.messages[0])
    assert "Detalhes do estado atualizado em 02/10/2026" in baseline
    assert "Fim do curso: 18/12/2026" in baseline
