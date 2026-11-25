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


@pytest.mark.parametrize("language", ["pt-BR", "en"])
def test_numbered_pages_keep_whole_blocks_and_order(tmp_path, language):
    path = tmp_path / "db"
    seed(path)
    event = saved_summary(path)
    summary = event["summary"]
    original = summary["eligible"]
    summary["eligible"] = [dict(original[i % 2], title=f"Turma {i:03} 🗓️ " + "á" * 47) for i in range(140)]
    summary["counts"].update(open=70, future=70)
    summary_parts = pages(event, language)
    assert len(summary_parts) >= 10
    bodies = []
    for i, part in enumerate(summary_parts, 1):
        prefix, body = part.split("\n", 1)
        assert prefix == f"{i}/{len(summary_parts)}"
        assert len(part.encode("utf-16-le")) <= 8192
        bodies.extend(body.split("\n\n"))
    assert bodies == blocks(event, language)
    sent = []
    def transport(req, limit):
        sent.append(json.loads(req.data)["text"])
        return b'{"ok":true,"result":{"message_id":1}}', {}
    assert Telegram("123:fake", "fixture", transport=transport, language=language).send(event)
    assert sent == summary_parts
    summary["eligible"][0]["title"] = "x" * 4096
    with pytest.raises(DeliveryUnavailable, match="bloco"):
        pages(event, language)


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
    assert "Detalhes atualizados em 02/10/2026" in baseline
    assert "Fim do curso: 18/12/2026" in baseline


@pytest.mark.parametrize("kind, heading", [
    ("registration_announced", "Upcoming registration"),
    ("registration_opened", "Registration open"),
    ("registration_reopened", "Registration reopened"),
    ("registration_closed", "Registration closed"),
    ("places_filled", "All places filled"),
])
def test_registration_headings_keep_source_fields(kind, heading):
    event = dict(kind=kind, title="Curso de Inglês", notice="42/2026",
                 course_state={"venue": "Casa Azul", "registration_mode": "Inscrição presencial"})
    text = message(event, "en")
    assert heading in text.splitlines()[0]
    assert "Course: Curso de Inglês" in text and "Venue: Casa Azul" in text
    assert "Registration method: in person" in text


def test_english_incidents_and_source_document():
    failure = dict(kind="monitoring_failure", after={"consecutive": 3, "reason": "HTTP timeout"})
    assert "Coletas consecutivas sem sucesso: 3." in message(failure)
    assert "Consecutive unsuccessful collections: 3." in message(failure, "en")
    assert "Reason: Request timed out" in message(failure, "en")
    assert "Monitoring recovered" in message({"kind": "monitoring_recovered"}, "en")
    doc = dict(kind="amendment", after={"label": "Retificação nº 5", "url": "https://example.invalid/5"})
    assert "Retificação nº 5\nDocument: https://example.invalid/5" in message(doc, "en")


def test_old_pending_baseline_keeps_original_course_title():
    event = dict(kind="baseline", after={"selections": 1, "courses": 1, "open_courses": ["Seleção"]})
    assert "Curso com inscrições abertas: Seleção" in message(event)
    assert "Course open for registration: Seleção" in message(event, "en")


def test_configured_language_at_delivery(tmp_path, monkeypatch):
    path = tmp_path / "db"
    seed(path)
    monkeypatch.setenv("BEACON_NOTIFICATION_LANGUAGE", "en")
    monkeypatch.setenv("BEACON_TELEGRAM_TOKEN", "123:fixture")
    monkeypatch.setenv("BEACON_TELEGRAM_CHAT_ID", "fixture")
    sent = []

    monkeypatch.setattr(Telegram, "_send_page", lambda self, text: sent.append(text))
    assert deliver_pending(path, Telegram.configured()) == 1
    assert "Initial summary · Santarém" in sent[0]
    assert "Selections: 2 · Courses: 4" in sent[0]
    assert "Open: 1 · Upcoming: 1 · Closed: 1" in sent[0]
    assert "Venue: Casa Azul" in sent[0]
    assert "2026-11-04 at 09:00" in sent[0]
    assert deliver_pending(path, Telegram.configured()) == 0

    monkeypatch.setenv("BEACON_DB_PATH", str(path))
    preview = subprocess.check_output([sys.executable, "-m", "oppy", "summary"], text=True)
    assert preview.rstrip() == message(saved_summary(path), "en")
