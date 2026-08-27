"""NSE calendar and snapshot validation (fail-closed) tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from qft.data.calendar import NSECalendar
from qft.data.validation import SnapshotValidator
from qft.domain.enums import DataQuality
from tests.conftest import TRADING_TS, make_quote

pytestmark = pytest.mark.unit


def test_trading_day_rules(calendar: NSECalendar) -> None:
    assert calendar.is_trading_day(date(2026, 8, 26))  # Wednesday
    assert not calendar.is_trading_day(date(2026, 8, 30))  # Sunday
    assert not calendar.is_trading_day(date(2026, 10, 2))  # Gandhi Jayanti


def test_market_open_ist_window(calendar: NSECalendar) -> None:
    assert calendar.is_market_open(TRADING_TS)  # 10:00 IST
    before_open = datetime(2026, 8, 26, 3, 30, tzinfo=UTC)  # 09:00 IST
    assert not calendar.is_market_open(before_open)
    after_close = datetime(2026, 8, 26, 10, 30, tzinfo=UTC)  # 16:00 IST
    assert not calendar.is_market_open(after_close)


def test_naive_ts_rejected(calendar: NSECalendar) -> None:
    with pytest.raises(ValueError):
        calendar.is_market_open(datetime(2026, 8, 26, 10, 0))


def test_fresh_snapshot_verifies(calendar: NSECalendar) -> None:
    v = SnapshotValidator(calendar)
    spot = make_quote("NSE:CASH:NIFTY", 24500.0)
    snap = v.build(TRADING_TS, "NIFTY", spot=spot)
    assert snap.verified
    assert snap.data_quality is DataQuality.GOOD
    assert snap.session_open


def test_stale_tick_fails_closed(calendar: NSECalendar) -> None:
    v = SnapshotValidator(calendar, max_tick_age_seconds=3.0)
    stale = make_quote("NSE:CASH:NIFTY", 24500.0, age_seconds=10.0)
    snap = v.build(TRADING_TS, "NIFTY", spot=stale)
    assert not snap.verified
    assert any("stale" in i for i in snap.issues)


def test_future_timestamp_fails(calendar: NSECalendar) -> None:
    v = SnapshotValidator(calendar)
    q = make_quote("NSE:CASH:NIFTY", 24500.0, age_seconds=-5.0)
    snap = v.build(TRADING_TS, "NIFTY", spot=q)
    assert not snap.verified
    assert any("future" in i for i in snap.issues)


def test_implausible_move_fails(calendar: NSECalendar) -> None:
    v = SnapshotValidator(calendar)
    snap1 = v.build(TRADING_TS, "NIFTY", spot=make_quote("NSE:CASH:NIFTY", 24500.0))
    assert snap1.verified
    snap2 = v.build(TRADING_TS, "NIFTY", spot=make_quote("NSE:CASH:NIFTY", 30000.0))
    assert not snap2.verified
    assert any("implausible" in i for i in snap2.issues)


def test_closed_session_never_verifies(calendar: NSECalendar) -> None:
    v = SnapshotValidator(calendar)
    sunday = datetime(2026, 8, 30, 4, 30, tzinfo=UTC)
    q = make_quote("NSE:CASH:NIFTY", 24500.0, as_of=sunday)
    snap = v.build(sunday, "NIFTY", spot=q)
    assert not snap.verified
    assert not snap.session_open


def test_empty_snapshot_is_bad(calendar: NSECalendar) -> None:
    v = SnapshotValidator(calendar)
    snap = v.build(TRADING_TS, "NIFTY")
    assert not snap.verified
    assert snap.data_quality is DataQuality.BAD
