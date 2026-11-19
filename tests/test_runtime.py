import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request

import pytest

from opportunity_beacon import runtime
from opportunity_beacon.backup import backup
from opportunity_beacon.core import record_collection
from opportunity_beacon.http_client import RequestFailure, request
from opportunity_beacon.outbox import DeliveryUnavailable, MemoryChannel, deliver_pending
from opportunity_beacon.psg import Outcome
from opportunity_beacon.telegram import Telegram, message

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        assert host == "127.0.0.1", "Tests may only resolve the loopback address"
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setenv("no_proxy", "127.0.0.1")


@contextmanager
def server(respond):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            respond(self)

        do_POST = do_GET

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as service:
        thread = threading.Thread(target=service.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield f"http://127.0.0.1:{service.server_port}"
        finally:
            service.shutdown()
            thread.join()


def html_for(url):
    files = {runtime.LISTING_URL: "psg-listing.html",
             runtime.ORIGIN + "/psg/selecao/detalhar/santarem/5/948": "psg-detail-152.html",
             runtime.ORIGIN + "/psg/selecao/detalhar/santarem/5/377342740": "psg-detail-126.html"}
    return (FIXTURES / files[url]).read_text()


def rows(path, query):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(query).fetchall()


class Stop:
    """Complete mocked waits immediately and stop after a fixed number of cycles."""
    def __init__(self, cycles):
        self.cycles = cycles
        self.delays = []

    def is_set(self):
        return len(self.delays) >= self.cycles

    def wait(self, delay):
        if delay == 1:
            return False
        self.delays.append(delay)
        return self.is_set()


@pytest.mark.parametrize("error", ["timeout", "500", "truncated", "redirect", "oversize", "403", "503"])
def test_transport_failure_preserves_state(tmp_path, monkeypatch, error):
    calls = []
    def respond(h):
        calls.append(h.headers["User-Agent"])
        if error == "timeout":
            time.sleep(0.15)
            return
        code = {"500": 500, "403": 403, "503": 503, "redirect": 302}.get(error, 200)
        h.send_response(code)
        h.send_header("Content-Type", "text/html")
        if error == "redirect":
            h.send_header("Location", "https://another.invalid/path")
        if error in ("truncated", "oversize"):
            h.send_header("Content-Length", "100" if error == "truncated" else "10000000")
        h.end_headers()
        h.wfile.write(b"short")

    path = tmp_path / "state.db"
    initial = runtime.collect(html_for)
    record_collection(path, initial.listing, initial.details)
    saved = rows(path, "SELECT snapshot FROM courses")
    with server(respond) as url:
        def transport(req, limit):
            local = Request(url, headers=dict(req.header_items()))
            return request(local, limit, total_timeout=0.05 if error == "timeout" else 2)
        monkeypatch.setattr(runtime, "request", transport)
        def fetch(link):
            return html_for(link) if link == runtime.LISTING_URL else runtime.fetch_page(link)
        obs = runtime.collect(fetch, stop=Stop(2))
    assert record_collection(path, obs.listing, obs.details) == Outcome.FAILED
    assert rows(path, "SELECT snapshot FROM courses") == saved
    assert len(calls) == (2 if error in ("500", "timeout") else 1)
    assert all(agent == runtime.USER_AGENT for agent in calls)
    expected = {"timeout": "timeout", "redirect": "302", "truncated": "Truncated", "oversize": "size limit"}
    assert expected.get(error, error) in obs.listing.reason


def test_scheduler_backoff_and_restart_health(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    attempts = []
    outcomes = iter(["blocked", "500", "absent", "blocked", "ok"])
    mode = None
    def fetch(url):
        nonlocal mode
        if url == runtime.LISTING_URL:
            mode = next(outcomes) if mode != "retry" else "failed"
            attempts.append(mode)
        if mode == "blocked":
            raise RequestFailure("HTTP 429", retry_after=5000)
        if mode == "500":
            mode = "retry"
            raise RequestFailure("HTTP 500", retryable=True)
        if mode == "failed":
            raise RequestFailure("HTTP 500")
        if mode == "absent":
            return html_for(url).replace("Santarém", "Belém")
        return html_for(url)

    clock = [100000.]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])

    class ClockStop(Stop):
        def wait(self, delay):
            clock[0] += delay
            return super().wait(delay)

    stop = ClockStop(5)
    channel = MemoryChannel()
    runtime.run_scheduler(path, channel, stop, interval=600, threshold=3, fetch=fetch)
    assert stop.delays == [5000, 2400, 4800, 9600, 600]
    assert attempts == ["blocked", "500", "failed", "absent", "blocked", "ok"]
    assert [m["kind"] for m in channel.messages] == ["monitoring_failure", "baseline", "monitoring_recovered"]


def test_429_header_and_restart_pause(tmp_path):
    path = tmp_path / "db"
    def respond(h):
        h.send_response(429)
        h.send_header("Retry-After", "9000")
        h.end_headers()
    with server(respond) as url:
        runtime.run_scheduler(path, None, Stop(1), interval=600, threshold=1,
                              fetch=lambda _: runtime.fetch_page(url))
    stop = Stop(1)
    runtime.run_scheduler(path, None, stop, interval=600, threshold=1,
                          fetch=lambda _: pytest.fail("Retry-After not honored after restart"))
    assert 8990 <= stop.delays[0] <= 9000


def test_immediate_single_scheduler_and_minimum_interval(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("BEACON_INTERVAL_MINUTES", "2")
    assert runtime.configuration()[0] == 600
    assert "minimum" in caplog.text
    path = tmp_path / "db"
    stop = Stop(1)
    def fetch(url):
        assert not stop.delays
        with pytest.raises(RuntimeError, match="already owns"):
            runtime.run_scheduler(path, None, Stop(1), interval=600, threshold=1,
                                  fetch=lambda _: pytest.fail("overlap"))
        return html_for(url)
    runtime.run_scheduler(path, None, stop, interval=600, threshold=1, fetch=fetch)
    assert rows(path, "SELECT outcome FROM collections") == [("OK",)]


def test_simulated_telegram_outage_and_single_summary(tmp_path, caplog):
    path = tmp_path / "db"
    received = []
    fake = "123:unit_test_only"
    def respond(h):
        received.append(json.loads(h.rfile.read(int(h.headers["Content-Length"]))))
        h.send_response(503 if len(received) == 1 else 200)
        h.end_headers()
        h.wfile.write(b'{"ok":true,"result":{"message_id":7}}')
    with server(respond) as url:
        def transport(req, limit):
            assert req.full_url.startswith("https://api.telegram.org/bot")
            return request(Request(url, data=req.data, headers=dict(req.header_items())), limit)
        channel = Telegram(fake, "test-chat", transport=transport)
        for attempt in (1, 2):
            runtime.run_scheduler(path, channel, Stop(1), interval=600, threshold=3, fetch=html_for)
            assert rows(path, "SELECT status, attempts FROM outbox") == [
                ("pending" if attempt == 1 else "sent", attempt)]
    assert len(received) == 2 and received[0] == received[1]
    assert "Resumo inicial" in received[0]["text"] and "4 curso(s)" in received[0]["text"]
    assert "parse_mode" not in received[0]
    assert rows(path, "SELECT outcome FROM collections") == [("OK",), ("OK",)]
    assert fake not in caplog.text and fake not in str(rows(path, "SELECT last_error FROM outbox"))
    def unsafe(req, limit):
        raise OSError(req.full_url)
    with pytest.raises(DeliveryUnavailable) as caught:
        Telegram(fake, "test-chat", transport=unsafe).send({"kind": "test"})
    assert fake not in "".join(traceback.format_exception(caught.value))


def test_ambiguous_streak_survives_restart_and_backup(tmp_path):
    path = tmp_path / "db"
    obs = runtime.collect(html_for)
    record_collection(path, obs.listing, obs.details)
    def ambiguous(url):
        return html_for(url).replace("Assistente de Recursos Humanos", "Possível troca de nome")
    bad = runtime.collect(ambiguous)
    for _ in range(4):
        assert record_collection(path, bad.listing, bad.details) == Outcome.INCONCLUSIVE
    record_collection(path, obs.listing, obs.details)
    assert rows(path, "SELECT kind FROM events ORDER BY id") == [
        ("baseline",), ("monitoring_failure",), ("monitoring_recovered",)]
    target = tmp_path / "backups" / "snapshot.db"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE courses SET last_alias='uncommitted'")
        backup(path, target)
        writer.rollback()
    assert rows(target, "PRAGMA integrity_check") == [("ok",)]
    assert rows(target, "SELECT last_alias FROM courses") == rows(path, "SELECT last_alias FROM courses")
    assert deliver_pending(target, MemoryChannel()) == 3
    assert rows(path, "SELECT count(*) FROM outbox WHERE status='pending'") == [(3,)]


def test_plain_text_fields():
    body = message({"kind": "new_course", "notice": "123/2026", "title": "A < B & C",
                    "url": "https://example.invalid/course", "after": {
                        "registration": "open", "registration_mode": "inscrição presencial",
                        "registration_end": "2026-11-19T18:00:00-03:00"}})
    assert "Curso: A < B & C" in body
    assert "19/11/2026 às 18:00" in body
    assert "Inscrições abertas" in body
    assert "Início do curso" not in body


def test_sigterm_and_status_without_credentials(tmp_path):
    path = tmp_path / "db"
    env = dict(os.environ, BEACON_DB_PATH=str(path), BEACON_TELEGRAM_TOKEN="", BEACON_TELEGRAM_CHAT_ID="")
    # Exercise CLI signal handlers with the transport replaced before CLI entry.
    code = """import sys
from pathlib import Path
from email.message import Message
from opportunity_beacon import runtime
from opportunity_beacon.__main__ import main
def offline(req, limit):
    names = {'selecao': 'listing', '948': 'detail-152', '377342740': 'detail-126'}
    name = names[req.full_url.rsplit('/', 1)[1]]
    headers = Message()
    headers['Content-Type'] = 'text/html'
    return Path('tests/fixtures', 'psg-' + name + '.html').read_bytes(), headers
runtime.request = offline
sys.argv = ['opportunity_beacon', 'run']
main()"""
    proc = subprocess.Popen([sys.executable, "-c", code], env=env)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                with closing(sqlite3.connect(path)) as db:
                    if (db.execute("SELECT 1 FROM sqlite_master WHERE name='outbox'").fetchone()
                            and db.execute("SELECT 1 FROM outbox").fetchone()):
                        break
            time.sleep(0.02)
        else:
            pytest.fail("Initial offline cycle did not finish")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=3) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    run = subprocess.run([sys.executable, "-m", "opportunity_beacon", "status"],
                         env=env, capture_output=True, text=True, check=True)
    assert "Telegram: not configured" in run.stdout and "Pending notifications: 1" in run.stdout
    target = tmp_path / "restore.sqlite3"
    subprocess.run([sys.executable, "-m", "opportunity_beacon", "backup", str(target)], env=env, check=True)
    assert rows(target, "SELECT outcome FROM collections") == [("OK",)]


def test_slow_request_backoff_cap_and_normal_interval_after_success(tmp_path, monkeypatch):
    clock = [0.]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    started = []
    def fetch(url):
        if url == runtime.LISTING_URL:
            started.append(clock[0])
            clock[0] += 1200
            if len(started) <= 7:
                raise RequestFailure("HTTP 403")
        return html_for(url)
    class VirtualStop(Stop):
        def wait(self, delay):
            clock[0] += delay
            return super().wait(delay)
    stop = VirtualStop(8)
    runtime.run_scheduler(tmp_path / "db", None, stop, interval=600, threshold=3, fetch=fetch)
    assert stop.delays == [1200, 2400, 4800, 9600, 19200, 21600, 21600, 600]
    assert all(b - a == 1200 + wait for a, b, wait in zip(started, started[1:], stop.delays))
