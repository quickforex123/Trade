"""Deterministic synthetic NIFTY intraday data for backtests and tests.

Synthetic data is for MECHANICS testing only — it must never be presented as
evidence that a strategy is profitable. Real evaluation requires real
historical data through the PIT store.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from qft.domain.market import Bar

SESSION_BARS = 75  # 09:15–15:30 IST in 5m bars


def synth_day(
    day: str,
    seed: int,
    open_price: float,
    drift: float = 0.0,
    vol: float = 0.0008,
    instrument_key: str = "NSE:FNO:NIFTYFUT",
) -> list[Bar]:
    """One 5m-bar session. IST 09:15 == 03:45 UTC."""
    rng = random.Random(f"{day}:{seed}")
    y, m, d = (int(x) for x in day.split("-"))
    start = datetime(y, m, d, 3, 45, tzinfo=UTC)
    bars: list[Bar] = []
    price = open_price
    for i in range(SESSION_BARS):
        ts = start + timedelta(minutes=5 * i)
        ret = rng.gauss(drift, vol)
        o = price
        c = max(1.0, price * (1 + ret))
        wick = abs(rng.gauss(0, vol / 2)) * price
        hi = max(o, c) + wick
        lo = min(o, c) - wick
        bars.append(
            Bar(
                instrument_key=instrument_key,
                ts=ts,
                interval="5m",
                open=round(o, 2),
                high=round(hi, 2),
                low=round(lo, 2),
                close=round(c, 2),
                volume=float(rng.randint(50_000, 200_000)),
            )
        )
        price = c
    return bars


def synth_history(
    n_days: int,
    seed: int = 42,
    start_price: float = 24_500.0,
    trend_days_frac: float = 0.4,
) -> dict[str, list[Bar]]:
    """Multi-day history mixing trending and choppy days, weekdays only."""
    rng = random.Random(seed)
    out: dict[str, list[Bar]] = {}
    price = start_price
    day = datetime(2026, 1, 5, tzinfo=UTC)  # a Monday
    made = 0
    while made < n_days:
        if day.weekday() < 5:
            key = day.date().isoformat()
            trending = rng.random() < trend_days_frac
            drift = rng.choice([1, -1]) * rng.uniform(0.0002, 0.0006) if trending else 0.0
            gap = rng.gauss(0, 0.003)
            price = max(1000.0, price * (1 + gap))
            bars = synth_day(key, seed + made, price, drift=drift)
            out[key] = bars
            price = bars[-1].close
            made += 1
        day += timedelta(days=1)
    return out
