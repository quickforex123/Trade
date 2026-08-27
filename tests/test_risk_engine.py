"""Risk firewall tests — every gate, plus property-based invariants."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from qft.config.risk_limits import RiskLimits
from qft.data.calendar import NSECalendar
from qft.data.validation import SnapshotValidator
from qft.domain.enums import Environment, KillSwitch, OrderType, Regime, Side
from qft.domain.risk import RiskReason
from qft.domain.signals import TradeIntent
from qft.risk.arming import ARM_PHRASE, LiveArming
from qft.risk.engine import PortfolioView, RiskEngine
from qft.risk.kill_switch import KillSwitchManager
from tests.conftest import TRADING_TS, make_quote

pytestmark = pytest.mark.unit

LIMITS = RiskLimits(initial_capital=50_000)
OPTION_KEY = "NSE:FNO:NIFTY26SEP24500CE"


def make_snapshot(calendar: NSECalendar, oi: float = 2_000_000, spread: tuple[float, float] = (84.9, 85.3)):
    v = SnapshotValidator(calendar)
    q = make_quote(OPTION_KEY, 85.0, bid=spread[0], ask=spread[1], oi=oi)
    return v.build(TRADING_TS, "NIFTY", quotes=(q,))


def make_intent(snapshot_id: str, **overrides) -> TradeIntent:
    base = dict(
        intent_id=overrides.pop("intent_id", "int_1"),
        strategy_id="orb",
        strategy_version="1.0",
        ts=TRADING_TS,
        signal_expiry=TRADING_TS + timedelta(seconds=20),
        underlying="NIFTY",
        instrument_key=OPTION_KEY,
        expiry="2026-09-01",
        strike=24500.0,
        option_type="CE",
        side=Side.BUY,
        lots=1,
        quantity=65,
        entry_type=OrderType.LIMIT,
        entry_price_limit=85.0,
        max_slippage_pct=0.01,
        stop_condition="premium <= 90",
        stop_loss_points=10.0,
        estimated_transaction_cost=60.0,
        estimated_max_loss=710.0,
        expected_reward=1300.0,
        quant_confidence=0.7,
        market_regime=Regime.BREAKOUT,
        snapshot_id=snapshot_id,
        reason_code="ORB_LONG",
    )
    base.update(overrides)
    return TradeIntent(**base)


def make_engine(calendar: NSECalendar, env: Environment = Environment.PAPER,
                limits: RiskLimits = LIMITS, armed=None) -> RiskEngine:
    return RiskEngine(
        limits=limits,
        calendar=calendar,
        kill_switch=KillSwitchManager(),
        environment=env,
        allowed_expiries_provider=lambda: [
            datetime(2026, 9, 1).date(),
            datetime(2026, 9, 8).date(),
            datetime(2026, 9, 29).date(),
        ],
        live_armed_check=armed,
    )


def healthy_portfolio(**overrides) -> PortfolioView:
    base = dict(
        equity=50_000.0,
        cash_available=40_000.0,
        margin_available=40_000.0,
        high_water_mark=50_000.0,
    )
    base.update(overrides)
    return PortfolioView(**base)


def test_healthy_intent_approved(calendar, nifty_option) -> None:
    snap = make_snapshot(calendar)
    assert snap.verified
    eng = make_engine(calendar)
    d = eng.evaluate(make_intent(snap.snapshot_id), snap, healthy_portfolio(), nifty_option, TRADING_TS)
    assert d.approved, d.detail
    assert d.reasons == (RiskReason.APPROVED,)


def test_duplicate_intent_rejected(calendar, nifty_option) -> None:
    snap = make_snapshot(calendar)
    eng = make_engine(calendar)
    i = make_intent(snap.snapshot_id)
    assert eng.evaluate(i, snap, healthy_portfolio(), nifty_option, TRADING_TS).approved
    d2 = eng.evaluate(i, snap, healthy_portfolio(), nifty_option, TRADING_TS)
    assert not d2.approved
    assert RiskReason.DUPLICATE_INTENT in d2.reasons


def test_expired_signal_rejected(calendar, nifty_option) -> None:
    snap = make_snapshot(calendar)
    eng = make_engine(calendar)
    late = TRADING_TS + timedelta(seconds=30)
    d = eng.evaluate(make_intent(snap.snapshot_id), snap, healthy_portfolio(), nifty_option, late)
    assert RiskReason.SIGNAL_EXPIRED in d.reasons


def test_unverified_snapshot_rejected(calendar, nifty_option) -> None:
    v = SnapshotValidator(calendar)
    stale_q = make_quote(OPTION_KEY, 85.0, bid=84.9, ask=85.3, oi=2e6, age_seconds=20)
    snap = v.build(TRADING_TS, "NIFTY", quotes=(stale_q,))
    assert not snap.verified
    eng = make_engine(calendar)
    d = eng.evaluate(make_intent(snap.snapshot_id), snap, healthy_portfolio(), None, TRADING_TS)
    assert not d.approved
    assert RiskReason.SNAPSHOT_UNVERIFIED in d.reasons


def test_missing_snapshot_fails_closed(calendar, nifty_option) -> None:
    eng = make_engine(calendar)
    d = eng.evaluate(make_intent("snap_x"), None, healthy_portfolio(), nifty_option, TRADING_TS)
    assert RiskReason.SNAPSHOT_MISSING in d.reasons


def test_hard_kill_blocks_everything(calendar, nifty_option) -> None:
    snap = make_snapshot(calendar)
    ks = KillSwitchManager()
    ks.trip(KillSwitch.HARD, "test")
    eng = RiskEngine(LIMITS, calendar, ks, Environment.PAPER)
    d = eng.evaluate(make_intent(snap.snapshot_id), snap, healthy_portfolio(), nifty_option,
                     TRADING_TS, is_exit=True)
    assert RiskReason.KILL_SWITCH_HARD in d.reasons


def test_soft_kill_blocks_entries_allows_exits(calendar, nifty_option) -> None:
    snap = make_snapshot(calendar)
    ks = KillSwitchManager()
    ks.trip(KillSwitch.SOFT, "daily loss")
    eng = RiskEngine(LIMITS, calendar, ks, Environment.PAPER,
                     allowed_expiries_provider=lambda: [datetime(2026, 9, 1).date()])
    entry = eng.evaluate(make_intent(snap.snapshot_id), snap, healthy_portfolio(), nifty_option, TRADING_TS)
    assert RiskReason.KILL_SWITCH_SOFT in entry.reasons
    exit_d = eng.evaluate(
        make_intent(snap.snapshot_id, intent_id="int_exit", side=Side.SELL),
        snap, healthy_portfolio(), nifty_option, TRADING_TS, is_exit=True,
    )
    assert RiskReason.KILL_SWITCH_SOFT not in exit_d.reasons


def test_live_unarmed_rejected(calendar, nifty_option, tmp_path) -> None:
    snap = make_snapshot(calendar)
    arming = LiveArming(tmp_path / "arm.json")
    eng = make_engine(calendar, env=Environment.LIVE, armed=arming.is_armed)
    d = eng.evaluate(make_intent(snap.snapshot_id), snap, healthy_portfolio(), nifty_option, TRADING_TS)
    assert RiskReason.ENV_NOT_ARMED in d.reasons
    arming.arm(ARM_PHRASE, "yash")
    d2 = eng.evaluate(make_intent(snap.snapshot_id, intent_id="int_2"), snap,
                      healthy_portfolio(), nifty_option, TRADING_TS)
    assert d2.approved, d2.detail


def test_arming_phrase_and_restart(tmp_path) -> None:
    a = LiveArming(tmp_path / "arm.json")
    with pytest.raises(ValueError):
        a.arm("yes please", "yash")
    a.arm(ARM_PHRASE, "yash")
    assert a.is_armed()
    # simulate restart: new instance, same file
    b = LiveArming(tmp_path / "arm.json")
    assert not b.is_armed()


def test_min_lot_exceeds_budget(calendar, nifty_option) -> None:
    snap = make_snapshot(calendar)
    eng = make_engine(calendar)
    # 1 lot with estimated max loss beyond 1.5% of 50k (750)
    d = eng.evaluate(
        make_intent(snap.snapshot_id, estimated_max_loss=900.0, expected_reward=2000.0),
        snap, healthy_portfolio(), nifty_option, TRADING_TS,
    )
    assert RiskReason.MIN_LOT_EXCEEDS_RISK_BUDGET in d.reasons


def test_daily_loss_headroom(calendar, nifty_option) -> None:
    snap = make_snapshot(calendar)
    eng = make_engine(calendar)
    p = healthy_portfolio(realized_pnl_today=-1200.0)  # budget 1500 → headroom 300 < 710
    d = eng.evaluate(make_intent(snap.snapshot_id), snap, p, nifty_option, TRADING_TS)
    assert RiskReason.DAILY_LOSS_LIMIT in d.reasons


def test_drawdown_halt(calendar, nifty_option) -> None:
    snap = make_snapshot(calendar)
    eng = make_engine(calendar)
    p = healthy_portfolio(equity=44_000.0, high_water_mark=50_000.0)  # 12% dd
    d = eng.evaluate(make_intent(snap.snapshot_id), snap, p, nifty_option, TRADING_TS)
    assert RiskReason.DRAWDOWN_HALT in d.reasons


def test_naked_short_option_forbidden(calendar, nifty_option) -> None:
    snap = make_snapshot(calendar)
    eng = make_engine(calendar)
    d = eng.evaluate(
        make_intent(snap.snapshot_id, side=Side.SELL, entry_price_limit=85.0),
        snap, healthy_portfolio(), nifty_option, TRADING_TS,
    )
    assert RiskReason.NAKED_SHORT_OPTION_FORBIDDEN in d.reasons


def test_spread_and_oi_gates(calendar, nifty_option) -> None:
    eng = make_engine(calendar)
    wide = make_snapshot(calendar, spread=(83.0, 87.0))
    d = eng.evaluate(make_intent(wide.snapshot_id), wide, healthy_portfolio(), nifty_option, TRADING_TS)
    assert RiskReason.SPREAD_TOO_WIDE in d.reasons
    thin = make_snapshot(calendar, oi=50_000)
    d2 = eng.evaluate(make_intent(thin.snapshot_id, intent_id="i2"), thin,
                      healthy_portfolio(), nifty_option, TRADING_TS)
    assert RiskReason.OI_TOO_LOW in d2.reasons


def test_exposure_and_frequency_gates(calendar, nifty_option) -> None:
    snap = make_snapshot(calendar)
    eng = make_engine(calendar)
    p = healthy_portfolio(open_position_count=1, open_underlyings=("NIFTY",), open_strategy_ids=("orb",))
    d = eng.evaluate(make_intent(snap.snapshot_id), snap, p, nifty_option, TRADING_TS)
    assert RiskReason.MAX_CONCURRENT_POSITIONS in d.reasons
    assert RiskReason.UNDERLYING_EXPOSURE_LIMIT in d.reasons

    eng2 = make_engine(calendar)
    eng2.record_order_submitted("o1", TRADING_TS - timedelta(seconds=5))
    eng2.record_order_submitted("o2", TRADING_TS - timedelta(seconds=3))
    d2 = eng2.evaluate(make_intent(snap.snapshot_id, intent_id="i9"), snap,
                       healthy_portfolio(), nifty_option, TRADING_TS)
    assert RiskReason.ORDER_FREQUENCY_LIMIT in d2.reasons

    d3 = make_engine(calendar).evaluate(
        make_intent(snap.snapshot_id, intent_id="i10"), snap,
        healthy_portfolio(orders_today=6), nifty_option, TRADING_TS,
    )
    assert RiskReason.DAILY_ORDER_LIMIT in d3.reasons


def test_reconciliation_and_broker_gates(calendar, nifty_option) -> None:
    snap = make_snapshot(calendar)
    eng = make_engine(calendar)
    d = eng.evaluate(make_intent(snap.snapshot_id), snap,
                     healthy_portfolio(reconciled=False, broker_connected=False),
                     nifty_option, TRADING_TS)
    assert RiskReason.RECONCILIATION_MISMATCH in d.reasons
    assert RiskReason.BROKER_DEGRADED in d.reasons


def test_expiry_allowlist(calendar, nifty_option) -> None:
    snap = make_snapshot(calendar)
    eng = make_engine(calendar)
    d = eng.evaluate(
        make_intent(snap.snapshot_id, expiry="2026-12-29"), snap,
        healthy_portfolio(), nifty_option, TRADING_TS,
    )
    assert RiskReason.EXPIRY_NOT_ALLOWED in d.reasons


# --- property-based invariants -------------------------------------------------

@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    equity=st.floats(min_value=1_000, max_value=200_000),
    pnl_today=st.floats(min_value=-10_000, max_value=10_000),
    est_loss=st.floats(min_value=1, max_value=20_000),
    hwm_factor=st.floats(min_value=1.0, max_value=1.5),
)
def test_no_approval_can_exceed_loss_headroom(equity, pnl_today, est_loss, hwm_factor) -> None:
    """INVARIANT: an approved entry's estimated max loss never exceeds the
    remaining daily headroom, the per-trade budget, or the drawdown limit."""
    calendar = NSECalendar()
    snap = make_snapshot(calendar)
    eng = make_engine(calendar)
    portfolio = healthy_portfolio(
        equity=equity,
        cash_available=equity,
        high_water_mark=equity * hwm_factor,
        realized_pnl_today=pnl_today,
    )
    intent = make_intent(
        snap.snapshot_id,
        estimated_max_loss=est_loss,
        expected_reward=est_loss * 2,
    )
    d = eng.evaluate(intent, snap, portfolio, _OPTION, TRADING_TS)
    if d.approved:
        per_trade = equity * LIMITS.max_capital_at_risk_per_trade_pct
        daily_headroom = LIMITS.max_daily_loss(equity) + min(pnl_today, 0.0)
        drawdown = (equity * hwm_factor - equity) / (equity * hwm_factor)
        assert est_loss <= per_trade + 1e-6
        assert est_loss <= daily_headroom + 1e-6
        assert drawdown < LIMITS.max_drawdown_halt_pct


from qft.domain.enums import Exchange, OptionType, Segment  # noqa: E402
from qft.domain.instruments import Instrument  # noqa: E402

_OPTION = Instrument(
    exchange=Exchange.NSE,
    segment=Segment.FNO,
    trading_symbol="NIFTY26SEP24500CE",
    underlying="NIFTY",
    instrument_type="CE",
    expiry="2026-09-01",
    strike=24500.0,
    option_type=OptionType.CE,
    lot_size=65,
)
