"""PIT store look-ahead guarantees, feature engine determinism, regime rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qft.data.pit_store import PITBarStore
from qft.domain.enums import Regime
from qft.domain.market import Bar
from qft.features.engine import FeatureEngine
from qft.regime.engine import RegimeEngine

pytestmark = pytest.mark.unit

SESSION_OPEN = datetime(2026, 8, 26, 3, 45, tzinfo=UTC)  # 09:15 IST


def _bars(n: int, start: datetime = SESSION_OPEN, drift: float = 0.0) -> list[Bar]:
    out = []
    price = 24500.0
    for i in range(n):
        ts = start + timedelta(minutes=5 * i)
        o = price
        price = price * (1 + drift) + ((-1) ** i) * 2.0
        c = price
        hi, lo = max(o, c) + 3, min(o, c) - 3
        out.append(
            Bar(
                instrument_key="NSE:FNO:NIFTYFUT",
                ts=ts,
                interval="5m",
                open=o,
                high=hi,
                low=lo,
                close=c,
                volume=1000.0,
            )
        )
    return out


def test_pit_store_asof_cut(tmp_path) -> None:
    store = PITBarStore(tmp_path)
    bars = _bars(10)
    assert store.append(bars) == 10
    as_of = SESSION_OPEN + timedelta(minutes=5 * 5)  # only bars closing <= this
    got = store.bars("NSE:FNO:NIFTYFUT", "5m", SESSION_OPEN, SESSION_OPEN + timedelta(hours=2), as_of)
    # bar i closes at open + 5*(i+1) minutes; bars 0..4 close by +25min
    assert len(got) == 5
    assert all(b.ts + timedelta(minutes=5) <= as_of for b in got)


def test_pit_store_idempotent_append(tmp_path) -> None:
    store = PITBarStore(tmp_path)
    bars = _bars(5)
    assert store.append(bars) == 5
    assert store.append(bars) == 0  # duplicates ignored
    far = SESSION_OPEN + timedelta(days=1)
    got = store.bars("NSE:FNO:NIFTYFUT", "5m", SESSION_OPEN, far, far)
    assert len(got) == 5


def test_feature_engine_rejects_leaked_bars() -> None:
    eng = FeatureEngine()
    bars = _bars(10)
    as_of = bars[-1].ts  # last bar is AT as_of -> leak
    with pytest.raises(ValueError, match="leaked"):
        eng.compute("NIFTY", as_of, bars)


def test_feature_engine_deterministic_digest() -> None:
    eng = FeatureEngine()
    bars = _bars(20)
    as_of = bars[-1].ts + timedelta(minutes=5)
    f1 = eng.compute("NIFTY", as_of, bars, prev_day_close=24450.0)
    f2 = eng.compute("NIFTY", as_of, bars, prev_day_close=24450.0)
    assert f1.digest == f2.digest
    assert f1.features == f2.features
    assert "vwap" in f1.features
    assert "atr_pct" in f1.features
    assert f1.input_end < as_of


def test_regime_trending_up() -> None:
    eng = FeatureEngine()
    bars = _bars(40, drift=0.0008)  # steady climb
    as_of = bars[-1].ts + timedelta(minutes=5)
    frame = eng.compute("NIFTY", as_of, bars)
    regime = RegimeEngine(hysteresis_bars=1).classify(frame)
    assert regime in (Regime.TRENDING_UP, Regime.BREAKOUT)


def test_regime_event_risk_wins() -> None:
    eng = FeatureEngine()
    bars = _bars(40, drift=0.0008)
    as_of = bars[-1].ts + timedelta(minutes=5)
    frame = eng.compute("NIFTY", as_of, bars)
    r = RegimeEngine(event_dates=frozenset({as_of.date().isoformat()}))
    assert r.classify(frame) is Regime.EVENT_RISK


def test_regime_hysteresis_blocks_flapping() -> None:
    eng = FeatureEngine()
    up = eng.compute(
        "NIFTY", _bars(40, drift=0.0008)[-1].ts + timedelta(minutes=5), _bars(40, drift=0.0008)
    )
    flat = eng.compute("NIFTY", _bars(40)[-1].ts + timedelta(minutes=5), _bars(40))
    r = RegimeEngine(hysteresis_bars=2)
    first = r.classify(up)
    assert first in (Regime.TRENDING_UP, Regime.BREAKOUT)
    # A single contradicting frame must not flip a directional regime to another directional one;
    # NO_TRADE is an immediate structural veto and applies at once.
    second = r.classify(flat)
    assert second in (first, Regime.NO_TRADE, Regime.LOW_VOLATILITY)
