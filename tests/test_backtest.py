"""Backtest engine honesty properties, metrics, and evaluation gates."""

from __future__ import annotations

import pytest

from qft.backtest.engine import BacktestConfig, BacktestEngine
from qft.backtest.evaluation import (
    SurvivalGates,
    evaluate_strategy,
    monte_carlo_drawdown,
    walk_forward_days,
)
from qft.backtest.metrics import compute_metrics
from qft.costs.model import CostModel, CostRates
from qft.domain.time import IST
from qft.features.engine import FeatureEngine
from qft.regime.engine import RegimeEngine
from qft.strategies.orb import OpeningRangeBreakout
from tests.synthetic import synth_history

pytestmark = pytest.mark.integration


def make_engine(cost_multiplier: float = 1.0) -> BacktestEngine:
    rates = CostRates()
    if cost_multiplier != 1.0:
        rates = CostRates(
            brokerage_flat_per_order=rates.brokerage_flat_per_order * cost_multiplier,
            stt_future_sell_pct=rates.stt_future_sell_pct * cost_multiplier,
            exch_txn_future_pct=rates.exch_txn_future_pct * cost_multiplier,
        )
    return BacktestEngine(
        strategy=OpeningRangeBreakout(),
        feature_engine=FeatureEngine(),
        regime_engine=RegimeEngine(hysteresis_bars=1),
        cost_model=CostModel(rates),
        config=BacktestConfig(
            initial_capital=500_000,  # sized so 1 futures lot fits the 1.5% risk budget
            slippage_pct=0.0003 * cost_multiplier,
        ),
    )


@pytest.fixture(scope="module")
def history():
    return synth_history(60, seed=11)


@pytest.fixture(scope="module")
def result(history):
    return make_engine().run(history)


def test_engine_runs_and_is_honest(history, result) -> None:
    assert result.days == 60
    # every trade entered strictly after the session's first bars (warmup)
    for t in result.trades:
        ist_entry = t.entry_ts.astimezone(IST)
        assert ist_entry.time().hour >= 9
        assert t.exit_ts > t.entry_ts
        # square-off: no position may survive past 15:30 IST
        assert t.exit_ts.astimezone(IST).time().hour < 16
        # costs always positive, net = gross - costs
        assert t.costs > 0
        assert t.net_pnl == pytest.approx(t.gross_pnl - t.costs, abs=0.05)


def test_no_trade_when_min_lot_exceeds_budget(history) -> None:
    """₹50k equity cannot risk a 65-lot NIFTY future within 1.5%: engine must
    generate signals but never trade."""
    engine = BacktestEngine(
        strategy=OpeningRangeBreakout(),
        feature_engine=FeatureEngine(),
        regime_engine=RegimeEngine(hysteresis_bars=1),
        cost_model=CostModel(),
        config=BacktestConfig(initial_capital=50_000),
    )
    r = engine.run(history)
    assert r.trades == []
    assert r.signals_skipped_sizing == r.signals_generated > 0


def test_stop_conservatism_and_exit_reasons(result) -> None:
    reasons = {t.exit_reason for t in result.trades}
    assert reasons <= {"STOP", "TARGET", "TIME", "SESSION_END", "KILL"}
    for t in result.trades:
        if t.exit_reason == "STOP":
            assert t.net_pnl < 0  # stops lose (plus slippage/costs)


def test_metrics_computation(result) -> None:
    m = compute_metrics(result.trades, result.equity_curve, 500_000, result.days)
    assert m.n_trades == len(result.trades)
    assert 0 <= m.win_rate <= 1
    assert m.max_drawdown >= 0
    assert m.net_pnl == pytest.approx(sum(t.net_pnl for t in result.trades), abs=0.5)
    if m.n_trades:
        assert m.turnover > 0
        assert m.largest_loss <= 0


def test_walk_forward_splits_never_leak() -> None:
    days = [f"2026-01-{d:02d}" for d in range(1, 29)]
    folds = walk_forward_days(days, n_folds=3)
    assert folds
    for train, test in folds:
        assert max(train) < min(test)  # strict temporal separation
        assert not set(train) & set(test)


def test_monte_carlo_drawdown(result) -> None:
    if not result.trades:
        pytest.skip("no trades in synthetic run")
    med, p95 = monte_carlo_drawdown(result.trades, 500_000)
    assert 0 <= med <= p95 <= 1


def test_evaluation_gates_insufficient_sample(history) -> None:
    engine = make_engine()
    tiny = {k: history[k] for k in sorted(history)[:5]}
    r = engine.run(tiny)
    ev = evaluate_strategy("orb_v1", [r], [r], 500_000)
    assert not ev.eligible
    assert any("insufficient OOS sample" in f for f in ev.gate_failures)


def test_evaluation_full_walk_forward(history) -> None:
    engine = make_engine()
    days = sorted(history)
    folds = walk_forward_days(days, n_folds=3)
    is_results, oos_results = [], []
    for train, test in folds:
        is_results.append(engine.run({d: history[d] for d in train}))
        oos_results.append(engine.run({d: history[d] for d in test}))
    x2 = make_engine(cost_multiplier=2.0).run(history)
    ev = evaluate_strategy(
        "orb_v1", oos_results, is_results, 500_000,
        gates=SurvivalGates(min_trades=5),  # synthetic data: relax sample gate only
        cost_x2_result=x2,
    )
    # We assert the MECHANISM works (gates evaluated, score in range) — not
    # that the strategy is good on synthetic noise.
    assert isinstance(ev.eligible, bool)
    if ev.eligible:
        assert 0 <= ev.score <= 1
        assert set(ev.components) >= {"oos_sortino", "calmar", "fold_consistency"}
    else:
        assert ev.gate_failures
