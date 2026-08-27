"""Shared fixtures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qft.data.calendar import NSECalendar
from qft.domain.enums import Exchange, OptionType, Segment
from qft.domain.instruments import Instrument
from qft.domain.market import FeedMeta, Quote

# 2026-08-26 is a Wednesday trading day; 10:00 IST == 04:30 UTC.
TRADING_TS = datetime(2026, 8, 26, 4, 30, tzinfo=UTC)


@pytest.fixture
def calendar() -> NSECalendar:
    return NSECalendar()


@pytest.fixture
def nifty_option() -> Instrument:
    return Instrument(
        exchange=Exchange.NSE,
        segment=Segment.FNO,
        trading_symbol="NIFTY26SEP24500CE",
        groww_symbol="NIFTY26SEP24500CE",
        exchange_token="12345",
        underlying="NIFTY",
        instrument_type="CE",
        expiry="2026-09-01",
        strike=24500.0,
        option_type=OptionType.CE,
        lot_size=65,
        tick_size=0.05,
    )


@pytest.fixture
def nifty_future() -> Instrument:
    return Instrument(
        exchange=Exchange.NSE,
        segment=Segment.FNO,
        trading_symbol="NIFTY26SEPFUT",
        groww_symbol="NIFTY26SEPFUT",
        exchange_token="54321",
        underlying="NIFTY",
        instrument_type="FUT",
        expiry="2026-09-29",
        lot_size=65,
        tick_size=0.05,
    )


def make_quote(
    instrument_key: str,
    ltp: float,
    bid: float | None = None,
    ask: float | None = None,
    as_of: datetime = TRADING_TS,
    age_seconds: float = 0.5,
    oi: float | None = None,
    volume: float | None = None,
) -> Quote:
    ts = as_of - timedelta(seconds=age_seconds)
    return Quote(
        instrument_key=instrument_key,
        meta=FeedMeta(source="test", receive_ts=ts, exchange_ts=ts),
        ltp=ltp,
        bid=bid,
        ask=ask,
        bid_qty=500 if bid else None,
        ask_qty=500 if ask else None,
        open_interest=oi,
        volume=volume,
    )
