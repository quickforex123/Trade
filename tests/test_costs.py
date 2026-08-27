"""Indian cost model tests — values checked to the paisa."""

from __future__ import annotations

import pytest

from qft.costs import CostModel
from qft.domain.enums import Side

pytestmark = pytest.mark.unit


def test_option_buy_leg_no_stt_no_selfside_stamp() -> None:
    m = CostModel()
    b = m.option_leg(Side.BUY, premium=100.0, quantity=65)
    assert b.turnover == 6500.0
    assert b.stt == 0.0  # STT on sell side only for options
    assert b.brokerage == pytest.approx(min(20.0, 0.0005 * 6500), abs=0.01)
    assert b.stamp_duty == pytest.approx(round(6500 * 0.00003, 2))
    assert b.total > 0


def test_option_sell_leg_has_stt() -> None:
    m = CostModel()
    s = m.option_leg(Side.SELL, premium=100.0, quantity=65)
    assert s.stt == pytest.approx(6.5)  # 0.1% of premium turnover
    assert s.stamp_duty == 0.0


def test_round_trip_cost_positive_and_symmetricish() -> None:
    m = CostModel()
    total = m.option_round_trip(Side.BUY, entry_premium=100.0, exit_premium=110.0, quantity=65)
    legs = m.option_leg(Side.BUY, 100.0, 65).total + m.option_leg(Side.SELL, 110.0, 65).total
    assert total == pytest.approx(legs, abs=0.02)
    # sanity: a 1-lot NIFTY option round trip costs a couple dozen rupees, not hundreds
    assert 5.0 < total < 100.0


def test_future_legs() -> None:
    m = CostModel()
    buy = m.future_leg(Side.BUY, price=24500.0, quantity=65)
    sell = m.future_leg(Side.SELL, price=24500.0, quantity=65)
    assert buy.stt == 0.0
    assert sell.stt == pytest.approx(round(24500 * 65 * 0.0002, 2))
    assert buy.stamp_duty > 0
    assert sell.stamp_duty == 0.0


def test_invalid_inputs_rejected() -> None:
    m = CostModel()
    with pytest.raises(ValueError):
        m.option_leg(Side.BUY, premium=-1, quantity=65)
    with pytest.raises(ValueError):
        m.future_leg(Side.BUY, price=0, quantity=65)
