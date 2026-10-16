"""Shared HTTP retry helper (stdlib only).

Retries 429 / 5xx / timeouts / connection errors with exponential backoff + full jitter,
honoring Retry-After when the server sends one. Other HTTP errors are returned to the caller
(status, headers, body) so it can decide (404 = gone, 401/403 = auth -> fatal).
"""
import random, socket, time, urllib.error, urllib.request

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


class AuthError(RuntimeError):
    """Cloudflare said 401/403: token missing, wrong, or under-scoped. Fatal for the whole run."""


def backoff(attempt, base=2.0, cap=60.0, retry_after=None):
    if retry_after:
        try:
            return min(cap, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def request(req, timeout=90, tries=5, retry_status=RETRY_STATUS, no_retry=lambda code: False, sleep=time.sleep):
    """Open `req` (urllib Request). Returns (status, headers, body). Raises RuntimeError after `tries`."""
    last = None
    for attempt in range(tries):
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            if e.code not in retry_status or no_retry(e.code):
                return e.code, e.headers, body
            last = f"HTTP {e.code}"
            ra = e.headers.get("Retry-After") if e.headers else None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            last = f"{type(e).__name__}: {str(e)[:100]}"
            ra = None
        if attempt < tries - 1:
            sleep(backoff(attempt, retry_after=ra))
    raise RuntimeError(f"{req.get_method()} {req.full_url.split('?')[0][:90]} failed after {tries} tries: {last}")
