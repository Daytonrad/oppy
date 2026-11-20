"""Bounded HTTP requests for the Linux runtime, executed on its main thread."""

import http.client
import signal
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime

USER_AGENT = "Oppy/0.1.0 (OpportunityBeacon; +https://github.com/kayo-willian/oppy)"
CONNECT_TIMEOUT = 5
TOTAL_TIMEOUT = 15
MAX_PAGE_BYTES = 2 * 1024 * 1024


class RequestFailure(Exception):
    def __init__(self, reason, *, retryable=False, retry_after=0):
        super().__init__(reason)
        self.retryable = retryable
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


@contextmanager
def _deadline(seconds):
    # A per-socket timeout alone cannot bound a slow trickle or a DNS lookup.
    def expired(signum, frame):
        raise TimeoutError("Total HTTP deadline exceeded")

    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def retry_seconds(value):
    if not value:
        return 0
    try:
        if value.strip().isascii() and value.strip().isdigit():
            digits = value.strip().lstrip("0") or "0"
            return int(digits) if len(digits) <= 15 else 10**15
        stamp = parsedate_to_datetime(value)
        return max(0, (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp() - time.time())
    except (OverflowError, TypeError, ValueError):
        return 0


def request(req, limit, *, total_timeout=TOTAL_TIMEOUT):
    """Return bytes and headers. Never propagate a URL, response body or credentials."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with _deadline(total_timeout), opener.open(req, timeout=CONNECT_TIMEOUT) as response:
            if response.status != 200:
                raise RequestFailure(f"HTTP {response.status}")
            length = response.headers.get("Content-Length")
            if length is not None:
                if not length.isascii() or not length.isdigit() or len(length) > 10:
                    raise RequestFailure("Invalid Content-Length")
                length = int(length)
                if length > limit:
                    raise RequestFailure("Response exceeds size limit")
            body = response.read(limit + 1)
            if len(body) > limit:
                raise RequestFailure("Response exceeds size limit")
            if length is not None and len(body) != length:
                raise RequestFailure("Truncated HTTP response")
            return body, response.headers
    except urllib.error.HTTPError as exc:
        with exc:
            error = RequestFailure(f"HTTP {exc.code}", retryable=exc.code in (500, 502, 504),
                                   retry_after=retry_seconds(exc.headers.get("Retry-After")))
    except http.client.IncompleteRead:
        error = RequestFailure("Truncated HTTP response")
    except (TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        error = RequestFailure("HTTP timeout" if isinstance(reason, TimeoutError) else "HTTP connection failed",
                               retryable=True)
    # Raise outside the handler so traceback chains cannot contain a credential-bearing URL.
    raise error
