"""Domain model contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qft.domain import (
    Direction,
    OrderState,
    Regime,
    RiskDecision,
    RiskReason,
    Signal,
    TradeIntent,
    deterministic_id,
)
from qft.domain.enums import OrderType, Side
from qft.domain.orders import can_transition
from qft.domain.time import ensure_utc

pytestmark = pytest.mark.unit

TS = datetime(2026, 8, 26, 4, 30, tzinfo=UTC)


def _intent(**overrides) -> TradeIntent:
    base = dict(
        intent_id="int_abc",
        strategy_id="orb",
        strategy_version="1.0",
        ts=TS,
        signal_expiry=TS + timedelta(seconds=20),
        underlying="NIFTY",
        instrument_key="NSE:FNO:NIFTY26SEP24500CE",
        expiry="2026-09-01",
        strike=24500.0,
        option_type="CE",
        side=Side.BUY,
        lots=1,
        quantity=65,
        entry_type=OrderType.LIMIT,
        entry_price_limit=100.0,
        max_slippage_pct=0.01,
        stop_condition="premium <= 90",
        stop_loss_points=10.0,
        estimated_transaction_cost=60.0,
        estimated_max_loss=710.0,
        expected_reward=1300.0,
        quant_confidence=0.7,
        market_regime=Regime.BREAKOUT,
        snapshot_id="snap_1",
        reason_code="ORB_LONG",
    )
    base.update(overrides)
    return TradeIntent(**base)


def test_deterministic_id_stable() -> None:
    a = deterministic_id("int", "orb", "NIFTY", TS.isoformat())
    b = deterministic_id("int", "orb", "NIFTY", TS.isoformat())
    assert a == b
    assert a != deterministic_id("int", "orb", "NIFTY", (TS + timedelta(seconds=1)).isoformat())


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 8, 26, 10, 0))


def test_intent_limit_requires_price() -> None:
    with pytest.raises(ValueError, match="entry_price_limit"):
        _intent(entry_price_limit=None)


def test_intent_expiry_after_ts() -> None:
    with pytest.raises(ValueError, match="signal_expiry"):
        _intent(signal_expiry=TS)


def test_intent_reward_risk() -> None:
    i = _intent()
    assert i.expected_reward_risk == pytest.approx(1300.0 / 710.0)


def test_signal_strength_bounds() -> None:
    with pytest.raises(ValueError):
        Signal(
            signal_id="s",
            strategy_id="x",
            strategy_version="1",
            ts=TS,
            underlying="NIFTY",
            direction=Direction.LONG,
            strength=1.5,
            regime=Regime.BREAKOUT,
        )


def test_risk_decision_consistency() -> None:
    with pytest.raises(ValueError):
        RiskDecision(
            decision_id="d",
            intent_id="i",
            ts=TS,
            approved=True,
            reasons=(RiskReason.DAILY_LOSS_LIMIT,),
        )
    with pytest.raises(ValueError):
        RiskDecision(decision_id="d", intent_id="i", ts=TS, approved=False, reasons=())
    ok = RiskDecision(
        decision_id="d", intent_id="i", ts=TS, approved=True, reasons=(RiskReason.APPROVED,)
    )
    assert ok.approved


def test_order_state_machine() -> None:
    assert can_transition(OrderState.CREATED, OrderState.SUBMITTED)
    assert can_transition(OrderState.SUBMITTED, OrderState.UNKNOWN)
    assert can_transition(OrderState.UNKNOWN, OrderState.FILLED)
    assert not can_transition(OrderState.FILLED, OrderState.CANCELLED)
    assert not can_transition(OrderState.CREATED, OrderState.FILLED)


def test_instrument_tick_rounding(nifty_option) -> None:
    assert nifty_option.round_to_tick(101.234) == 101.25
    assert nifty_option.round_to_tick(101.22) == 101.20
