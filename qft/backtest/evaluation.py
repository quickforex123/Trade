"""Two-stage institutional evaluation.

STAGE A — hard survival gates: fail any ⇒ NOT ELIGIBLE, no score.
STAGE B — multi-objective normalized score with explicit penalties.

The optimizer is never rewarded for being active; a strategy that trades less
but survives out-of-sample outranks a spectacular unstable one.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import numpy as np
from pydantic import BaseModel, ConfigDict

from qft.backtest.engine import BacktestResult
from qft.backtest.metrics import Metrics, compute_metrics
from qft.domain.portfolio import TradeRecord


class SurvivalGates(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_trades: int = 30
    max_drawdown: float = 0.15
    max_single_day_pnl_share: float = 0.40  # no day may contribute > 40% of net profit
    min_oos_folds_profitable_frac: float = 0.5
    max_oos_degradation: float = 0.7  # OOS expectancy must be >= (1-x) * IS expectancy floor
    min_profit_factor: float = 1.05
    max_cost_sensitivity_break: float = 2.0  # must survive costs x2


class ScoreWeights(BaseModel):
    model_config = ConfigDict(frozen=True)

    oos_sortino: float = 0.30
    calmar: float = 0.25
    fold_consistency: float = 0.15
    after_cost_return: float = 0.10
    tail_quality: float = 0.10
    parameter_robustness: float = 0.10


@dataclass
class FoldResult:
    fold: int
    is_train: bool
    metrics: Metrics


@dataclass
class StrategyEvaluation:
    strategy_id: str
    eligible: bool
    gate_failures: list[str] = field(default_factory=list)
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    fold_results: list[FoldResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def walk_forward_days(days: list[str], n_folds: int, train_frac: float = 0.7) -> list[tuple[list[str], list[str]]]:
    """Rolling walk-forward: each fold trains on a window and tests on the
    following unseen window. Test windows never overlap training windows."""
    if n_folds < 1 or not days:
        return []
    days = sorted(days)
    fold_len = len(days) // (n_folds + 1)
    if fold_len < 2:
        return []
    folds = []
    for k in range(n_folds):
        train_end = fold_len * (k + 1)
        test_end = min(fold_len * (k + 2), len(days))
        train = days[:train_end]
        test = days[train_end:test_end]
        if test:
            folds.append((train, test))
    return folds


def monte_carlo_drawdown(trades: list[TradeRecord], initial_capital: float,
                         n_paths: int = 500, seed: int = 7) -> tuple[float, float]:
    """Reshuffle trade order to estimate drawdown distribution.
    Returns (median_max_dd, p95_max_dd)."""
    if not trades:
        return 0.0, 0.0
    rng = random.Random(seed)
    pnls = [t.net_pnl for t in trades]
    dds = []
    for _ in range(n_paths):
        rng.shuffle(pnls)
        equity = initial_capital
        peak = equity
        max_dd = 0.0
        for p in pnls:
            equity += p
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)
        dds.append(max_dd)
    arr = np.array(dds)
    return float(np.median(arr)), float(np.percentile(arr, 95))


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def evaluate_strategy(
    strategy_id: str,
    oos_results: list[BacktestResult],
    is_results: list[BacktestResult],
    initial_capital: float,
    gates: SurvivalGates | None = None,
    weights: ScoreWeights | None = None,
    param_sensitivity_scores: list[float] | None = None,
    cost_x2_result: BacktestResult | None = None,
) -> StrategyEvaluation:
    """Evaluate walk-forward results. `oos_results` are per-fold TEST windows,
    `is_results` the corresponding TRAIN windows."""
    gates = gates or SurvivalGates()
    weights = weights or ScoreWeights()
    ev = StrategyEvaluation(strategy_id=strategy_id, eligible=True)

    all_oos_trades = [t for r in oos_results for t in r.trades]
    all_oos_curve = [pt for r in oos_results for pt in r.equity_curve]
    n_days = sum(r.days for r in oos_results)
    oos_metrics = compute_metrics(all_oos_trades, all_oos_curve, initial_capital, max(n_days, 1))

    # ---- STAGE A: survival gates ----
    def gate(cond: bool, msg: str) -> None:
        if not cond:
            ev.eligible = False
            ev.gate_failures.append(msg)

    gate(len(all_oos_trades) >= gates.min_trades,
         f"insufficient OOS sample: {len(all_oos_trades)} < {gates.min_trades}")
    gate(oos_metrics.max_drawdown <= gates.max_drawdown,
         f"OOS drawdown {oos_metrics.max_drawdown:.1%} > {gates.max_drawdown:.1%}")
    gate(oos_metrics.profit_factor >= gates.min_profit_factor,
         f"OOS profit factor {oos_metrics.profit_factor:.2f} < {gates.min_profit_factor}")

    # single-day dependence
    if all_oos_trades and oos_metrics.net_pnl > 0:
        by_day: dict[str, float] = {}
        for t in all_oos_trades:
            by_day[t.exit_ts.date().isoformat()] = by_day.get(t.exit_ts.date().isoformat(), 0.0) + t.net_pnl
        best_day = max(by_day.values())
        gate(best_day / oos_metrics.net_pnl <= gates.max_single_day_pnl_share,
             f"one day contributes {best_day / oos_metrics.net_pnl:.0%} of net profit")

    # fold consistency
    profitable_folds = sum(1 for r in oos_results if sum(t.net_pnl for t in r.trades) > 0)
    fold_frac = profitable_folds / len(oos_results) if oos_results else 0.0
    gate(fold_frac >= gates.min_oos_folds_profitable_frac,
         f"only {profitable_folds}/{len(oos_results)} OOS folds profitable")

    # in-sample vs out-of-sample degradation
    all_is_trades = [t for r in is_results for t in r.trades]
    if all_is_trades:
        is_exp = sum(t.net_pnl for t in all_is_trades) / len(all_is_trades)
        oos_exp = oos_metrics.expectancy
        if is_exp > 0:
            degradation = 1 - (oos_exp / is_exp)
            gate(degradation <= gates.max_oos_degradation,
                 f"OOS expectancy degraded {degradation:.0%} vs in-sample")

    # cost sensitivity: strategy must survive doubled frictions
    if cost_x2_result is not None:
        x2_net = sum(t.net_pnl for t in cost_x2_result.trades)
        gate(x2_net > -initial_capital * 0.02,
             f"strategy collapses under 2x costs (net {x2_net:.0f})")

    if not ev.eligible:
        return ev

    # ---- STAGE B: multi-objective score ----
    mc_median_dd, mc_p95_dd = monte_carlo_drawdown(all_oos_trades, initial_capital)
    ev.notes.append(f"MC drawdown median={mc_median_dd:.1%} p95={mc_p95_dd:.1%}")

    c: dict[str, float] = {}
    c["oos_sortino"] = _clip01(oos_metrics.sortino / 3.0)
    c["calmar"] = _clip01(oos_metrics.calmar / 3.0)
    c["fold_consistency"] = _clip01(fold_frac)
    c["after_cost_return"] = _clip01(oos_metrics.total_return / 0.10)  # 10% OOS return = full marks
    tail = _clip01(1.0 + oos_metrics.cvar_95 / 0.03)  # CVaR −3%/day ⇒ 0
    c["tail_quality"] = tail
    c["parameter_robustness"] = (
        _clip01(float(np.mean(param_sensitivity_scores))) if param_sensitivity_scores else 0.5
    )

    score = (
        weights.oos_sortino * c["oos_sortino"]
        + weights.calmar * c["calmar"]
        + weights.fold_consistency * c["fold_consistency"]
        + weights.after_cost_return * c["after_cost_return"]
        + weights.tail_quality * c["tail_quality"]
        + weights.parameter_robustness * c["parameter_robustness"]
    )

    # explicit penalties
    penalties = 0.0
    penalties += 0.5 * max(0.0, oos_metrics.max_drawdown - 0.08)  # dd beyond 8%
    penalties += 0.3 * max(0.0, mc_p95_dd - 0.15)  # tail-path drawdown
    penalties += 0.1 * _clip01(oos_metrics.turnover / 500.0)  # churn
    total_costs = sum(t.costs for t in all_oos_trades)
    if oos_metrics.net_pnl > 0:
        penalties += 0.2 * _clip01(total_costs / max(oos_metrics.net_pnl + total_costs, 1e-9))
    trades_per_day = oos_metrics.n_trades / max(oos_metrics.n_days, 1)
    penalties += 0.1 * max(0.0, trades_per_day - 3.0) / 3.0  # excessive frequency
    left_skew = oos_metrics.avg_loss and abs(oos_metrics.largest_loss) / max(abs(oos_metrics.avg_loss), 1e-9)
    if left_skew and left_skew > 5:
        penalties += 0.05 * math.log(left_skew / 5)

    ev.score = max(0.0, round(score - penalties, 4))
    ev.components = {**c, "penalties": round(penalties, 4)}
    return ev
