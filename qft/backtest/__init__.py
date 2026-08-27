"""Backtesting: event-driven intraday simulator, metrics, survival gates."""

from qft.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from qft.backtest.evaluation import StrategyEvaluation, evaluate_strategy
from qft.backtest.metrics import compute_metrics

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "StrategyEvaluation",
    "compute_metrics",
    "evaluate_strategy",
]
