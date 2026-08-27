"""Timezone-aware time helpers. All stored timestamps are UTC; IST is a view."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_utc() -> datetime:
    return datetime.now(UTC)


def ist_now() -> datetime:
    return datetime.now(IST)


def to_ist(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime rejected: all timestamps must be tz-aware")
    return ts.astimezone(IST)


def ensure_utc(ts: datetime) -> datetime:
    """Reject naive datetimes; normalize aware ones to UTC."""
    if ts.tzinfo is None:
        raise ValueError("naive datetime rejected: all timestamps must be tz-aware")
    return ts.astimezone(UTC)
