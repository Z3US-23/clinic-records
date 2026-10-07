"""Slow down password guessing on the sign-in pages (the app's own and the Django admin's).

Failed sign-ins are counted in the cache in three ways, each with its own limit:

    email + IP address     settings.LOGIN_MAX_ATTEMPTS            one account, guessed (or mistyped)
                                                                  from one place
    email, any address     settings.LOGIN_MAX_ATTEMPTS_PER_EMAIL  one account, guessed from many
                                                                  addresses
    IP address, any email  settings.LOGIN_MAX_ATTEMPTS_PER_IP     one address trying many accounts
                                                                  ("password spraying")

IPv6 addresses are counted per /64 network: one home or mobile connection usually has a
whole /64 to itself, so it could otherwise use a new address for every guess.

When any counter reaches its limit within settings.LOGIN_LOCKOUT_MINUTES, the sign-ins it
covers are refused for LOGIN_LOCKOUT_MINUTES and the password is not even checked.
A successful sign-in clears only the email + IP counter. The wider counters wear off with
time, so signing in to an account you own does not reset your count of guesses at others.

The per-email limit lets someone who knows a doctor's email lock that account for a while
(from several addresses). That is why it sits well above the email + IP limit.

Notes for running it:
  * The cache must be shared by all server processes for the counts to be exact
    (Redis or the database cache in production). With the default per-process memory
    cache each worker keeps its own count.
  * Behind a proxy, set DJANGO_USE_X_FORWARDED_FOR. Otherwise every visitor seems to come
    from the proxy's address and the per-address limit would lock everyone out at once.
"""

import hashlib
import ipaddress
import math
import time

from django.conf import settings
from django.core.cache import cache


def _now():
    """Current time in seconds. A separate function so tests can move the clock."""
    return time.time()


def _window_seconds():
    return settings.LOGIN_LOCKOUT_MINUTES * 60


def _address_group(ip):
    """What to count an address by: an IPv4 address as it is, an IPv6 address's /64 network."""
    if not ip:
        return ""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return str(ip)
    if address.version == 6 and address.ipv4_mapped:
        # "::ffff:1.2.3.4" is how dual-stack servers report an IPv4 visitor: count the IPv4
        # address, or every IPv4 visitor would share one /64.
        return str(address.ipv4_mapped)
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def _cache_key(kind, *parts):
    # Hash so any email/IP text gives a short, cache-safe key.
    raw = "|".join(parts)
    return f"login-failures:{kind}:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pair_key(email, ip):
    return _cache_key("pair", (email or "").strip().lower(), _address_group(ip))


def _counters(email, ip):
    """(cache key, limit) for every counter that covers a sign-in for this email from this address."""
    email = (email or "").strip().lower()
    address = _address_group(ip)
    counters = [
        (_pair_key(email, ip), settings.LOGIN_MAX_ATTEMPTS),
        (_cache_key("email", email), settings.LOGIN_MAX_ATTEMPTS_PER_EMAIL),
    ]
    if address:
        counters.append((_cache_key("ip", address), settings.LOGIN_MAX_ATTEMPTS_PER_IP))
    return counters


def _seconds_left(key):
    data = cache.get(key)
    if not data:
        return 0
    return max(0, math.ceil(data.get("locked_until", 0) - _now()))


def _count_failure(key, limit):
    """Add one failure to a counter; lock it once the limit is reached."""
    now = _now()
    window = _window_seconds()
    data = cache.get(key)
    if not data or now - data["first_failure"] > window:
        # Start a fresh counting window.
        data = {"count": 0, "first_failure": now, "locked_until": 0}
    data["count"] += 1
    if data["count"] >= limit:
        data["locked_until"] = now + window
    cache.set(key, data, timeout=window)


def seconds_locked(email, ip):
    """How many seconds a sign-in for this email from this address must still wait (0 = not locked)."""
    return max(_seconds_left(key) for key, _limit in _counters(email, ip))


def minutes_locked(email, ip):
    """Remaining lock time rounded up to whole minutes (0 = not locked)."""
    return math.ceil(seconds_locked(email, ip) / 60)


def record_failure(email, ip):
    """Count one failed sign-in on every counter that covers it."""
    for key, limit in _counters(email, ip):
        _count_failure(key, limit)


def clear(email, ip):
    """Forget earlier failures for this email + address (called after a successful sign-in).

    The per-email and per-address counters are left to wear off: see the module docstring.
    """
    cache.delete(_pair_key(email, ip))
