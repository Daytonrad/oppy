"""One source, one scheduler and one sequential delivery worker."""

import fcntl
import logging
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from threading import Event
from urllib.request import Request

from . import initialize
from .core import record_collection
from .http_client import MAX_PAGE_BYTES, USER_AGENT, RequestFailure, request
from .snapshots import save_snapshot
from .outbox import deliver_pending
from .psg import ORIGIN, Outcome, ParseResult, parse_detail, parse_listing

LISTING_URL = ORIGIN + "/psg/selecao"
DEFAULT_INTERVAL = 30
MIN_INTERVAL = 10
BACKOFF_CEILING = 6 * 3600
LOG = logging.getLogger(__name__)


def configuration():
    minutes = int(os.getenv("BEACON_INTERVAL_MINUTES", DEFAULT_INTERVAL))
    threshold = int(os.getenv("BEACON_FAILURE_ALERT_AFTER", "3"))
    if threshold < 1:
        raise ValueError("BEACON_FAILURE_ALERT_AFTER must be positive")
    if minutes < MIN_INTERVAL:
        LOG.warning("BEACON_INTERVAL_MINUTES below minimum; using %s minutes", MIN_INTERVAL)
    return max(MIN_INTERVAL, minutes) * 60, threshold


@dataclass
class HtmlPage:
    text: str
    raw: bytes


def fetch_page(url):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    body, headers = request(req, MAX_PAGE_BYTES)
    if headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
        raise RequestFailure("Expected an HTML response")
    try:
        return HtmlPage(body.decode(headers.get_content_charset() or "utf-8"), body)
    except (UnicodeError, LookupError):
        raise RequestFailure("Invalid HTML encoding") from None


@dataclass
class Observation:
    listing: ParseResult
    details: tuple = ()
    retry_after: float = 0
    failed_url: str | None = None


class Stopped(Exception):
    pass


def collect(fetch=fetch_page, *, stop=None, retry=True, snapshot_dir=None):
    stop = stop or Event()

    def page(url):
        if stop.is_set():
            raise Stopped
        try:
            return fetch(url)
        except RequestFailure as error:
            if not retry or not error.retryable or error.retry_after:
                raise
        if stop.wait(1):
            raise Stopped
        return fetch(url)

    def parse_page(url, kind, parser, *args):
        response = page(url)
        # Injected fixture transports can supply decoded text directly.
        if isinstance(response, str):
            response = HtmlPage(response, response.encode())
        result = parser(response.text, *args)
        if result.status != Outcome.OK:
            save_snapshot(snapshot_dir, kind, url, response.raw, result)
        return result

    url = LISTING_URL
    try:
        listing = parse_page(url, "listing", parse_listing)
        if listing.status != Outcome.OK:
            return Observation(listing, failed_url=url)
        details = []
        for selection in listing.value or ():
            url = selection.url
            result = parse_page(url, "detail", parse_detail, selection)
            details.append(result)
            if result.status != Outcome.OK:
                return Observation(listing, tuple(details), failed_url=url)
        return Observation(listing, tuple(details))
    except RequestFailure as error:
        return Observation(ParseResult(Outcome.FAILED, str(error)), retry_after=error.retry_after, failed_url=url)


def run_scheduler(path, channel, stop, *, interval, threshold, fetch=fetch_page):
    initialize(path)
    with open(str(path.resolve()) + ".lock", "a") as lock, closing(sqlite3.connect(path)) as db:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A scheduler already owns this database") from None
        previous = db.execute("SELECT retry_not_before FROM collections ORDER BY id DESC LIMIT 1").fetchone()
        if previous and previous[0] and stop.wait(max(0, previous[0] - time.time())):
            return
        while not stop.is_set():
            try:
                obs = collect(fetch, stop=stop, snapshot_dir=path.parent / "failed-snapshots")
            except Stopped:
                return
            gate = time.time() + obs.retry_after if obs.retry_after else None
            outcome = record_collection(path, obs.listing, obs.details, failure_threshold=threshold,
                                        retry_not_before=gate)
            reason, = db.execute("SELECT reason FROM collections ORDER BY id DESC LIMIT 1").fetchone()
            delay = interval
            if outcome != Outcome.OK:
                count, = db.execute("""SELECT count(*) FROM collections WHERE id >
                                    coalesce((SELECT max(id) FROM collections WHERE outcome='OK'),0)""").fetchone()
                delay = max(interval, min(BACKOFF_CEILING, interval * 2 ** min(count, 20)))
                LOG.warning("Collection %s: %s", outcome, reason)
            else:
                LOG.info("Collection OK: %s", reason)
            if channel and not stop.is_set():
                deliver_pending(path, channel, should_stop=stop.is_set)
            if gate:
                delay = max(delay, gate - time.time())
            # Wait in bounded pieces for very long server-requested pauses.
            until = time.monotonic() + delay
            while delay > 0 and not stop.is_set():
                if stop.wait(min(delay, 86400)):
                    return
                delay = until - time.monotonic()
