"""Slow down password guessing on the sign-in page.

Failed sign-ins are counted in the cache per (email, IP address). After
settings.LOGIN_MAX_ATTEMPTS failures within settings.LOGIN_LOCKOUT_MINUTES,
that email + IP pair is locked for LOGIN_LOCKOUT_MINUTES and the password is
not even checked. A successful sign-in clears the counter.

Note: the cache must be shared by all server processes for this to be exact
(e.g. Redis or the database cache in production). With the default
per-process memory cache each worker keeps its own count.
"""

import hashlib
import math
import time

from django.conf import settings
from django.core.cache import cache


def _now():
    """Current time in seconds. A separate function so tests can move the clock."""
    return time.time()


def _cache_key(email, ip):
    # Hash so any email/IP text gives a short, cache-safe key.
    raw = f"{(email or '').strip().lower()}|{ip or ''}"
    return "login-failures:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _window_seconds():
    return settings.LOGIN_LOCKOUT_MINUTES * 60


def seconds_locked(email, ip):
    """How many seconds this email + IP must still wait (0 = not locked)."""
    data = cache.get(_cache_key(email, ip))
    if not data:
        return 0
    remaining = data.get("locked_until", 0) - _now()
    return max(0, math.ceil(remaining))


def minutes_locked(email, ip):
    """Remaining lock time rounded up to whole minutes (0 = not locked)."""
    return math.ceil(seconds_locked(email, ip) / 60)


def record_failure(email, ip):
    """Count one failed sign-in; lock the pair once the limit is reached."""
    key = _cache_key(email, ip)
    now = _now()
    window = _window_seconds()
    data = cache.get(key)
    if not data or now - data["first_failure"] > window:
        # Start a fresh counting window.
        data = {"count": 0, "first_failure": now, "locked_until": 0}
    data["count"] += 1
    if data["count"] >= settings.LOGIN_MAX_ATTEMPTS:
        data["locked_until"] = now + window
    cache.set(key, data, timeout=window)


def clear(email, ip):
    """Forget earlier failures (called after a successful sign-in)."""
    cache.delete(_cache_key(email, ip))
