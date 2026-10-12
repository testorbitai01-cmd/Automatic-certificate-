"""Database-backed fixed-window rate limiting.

Counters live in PostgreSQL, so every gunicorn worker and container sees the same counts and
they survive restarts (unlike the per-process local-memory cache). Increments are a single
atomic UPDATE, so concurrent requests cannot both slip under the limit.

Identifiers are hashed before storage; raw emails and IP addresses are not kept.
"""
import datetime as dt
import hashlib
import random

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .models import RateLimitCounter

PURGE_PROBABILITY = 0.01


def _key(scope, ident):
    return hashlib.sha256(f"{scope}|{ident}".encode()).hexdigest()


def _window(window_seconds, now=None):
    now = now or timezone.now()
    start = int(now.timestamp()) // window_seconds * window_seconds
    window_start = dt.datetime.fromtimestamp(start, tz=dt.timezone.utc)
    return window_start, window_start + dt.timedelta(seconds=window_seconds)


def count(scope, ident, window_seconds):
    window_start, _ = _window(window_seconds)
    row = RateLimitCounter.objects.filter(key=_key(scope, ident), window_start=window_start).first()
    return row.count if row else 0


def _increment(key, window_start):
    """Atomically add one and return the new count, or None if the row does not exist yet.

    UPDATE ... RETURNING makes the increment and the read a single statement, so each concurrent
    caller sees its own exact position (PostgreSQL; SQLite >= 3.35).
    """
    table = connection.ops.quote_name(RateLimitCounter._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {table} SET count = count + 1 WHERE key = %s AND window_start = %s RETURNING count",
            # Adapt the datetime the way the ORM stores it (SQLite keeps datetimes as text).
            [key, connection.ops.adapt_datetimefield_value(window_start)],
        )
        row = cursor.fetchone()
    return row[0] if row else None


def hit(scope, ident, window_seconds):
    """Record one event and return the number of events in the current window (including this one)."""
    key = _key(scope, ident)
    window_start, expires_at = _window(window_seconds)
    new_count = _increment(key, window_start)
    if new_count is None:
        try:
            with transaction.atomic():
                RateLimitCounter.objects.create(key=key, window_start=window_start, expires_at=expires_at, count=1)
            new_count = 1
        except IntegrityError:
            new_count = _increment(key, window_start)  # another process created the row first
    if random.random() < PURGE_PROBABILITY:
        purge_expired()
    return new_count


def reset(scope, ident):
    RateLimitCounter.objects.filter(key=_key(scope, ident)).delete()


def purge_expired():
    return RateLimitCounter.objects.filter(expires_at__lt=timezone.now()).delete()[0]
