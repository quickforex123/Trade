"""Performance metrics over NET (after-cost) results."""

from __future__ import annotations

import math

import numpy as np
from pydantic import BaseModel, ConfigDict

from qft.domain.portfolio import TradeRecord

TRADING_DAYS = 252


class Metrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    n_trades: int
    n_days: int
    net_pnl: float
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    max_drawdown_duration_days: int
    profit_factor: float
    win_rate: float
    expectancy: float
    avg_win: float
    avg_loss: float
    cvar_95: float  # expected shortfall of daily returns (negative = loss)
    var_95: float
    turnover: float  # notional traded / initial capital
    avg_mae: float
    avg_mfe: float
    largest_loss: float
    time_in_market_frac: float


def daily_returns(equity_curve: list[tuple[object, float]], initial: float) -> np.ndarray:
    values = np.array([initial] + [v for _, v in equity_curve], dtype=float)
    if len(values) < 2:
        return np.array([])
    return np.diff(values) / values[:-1]


def compute_metrics(
    trades: list[TradeRecord],
    equity_curve: list[tuple[object, float]],
    initial_capital: float,
    n_days: int,
) -> Metrics:
    rets = daily_returns(equity_curve, initial_capital)
    net = sum(t.net_pnl for t in trades)
    final_equity = equity_curve[-1][1] if equity_curve else initial_capital
    total_return = final_equity / initial_capital - 1.0

    years = max(n_days / TRADING_DAYS, 1e-9)
    cagr = (final_equity / initial_capital) ** (1 / years) - 1 if final_equity > 0 else -1.0

    if len(rets) >= 2 and np.std(rets, ddof=1) > 0:
        sharpe = float(np.mean(rets) / np.std(rets, ddof=1) * math.sqrt(TRADING_DAYS))
    else:
        sharpe = 0.0
    downside = rets[rets < 0]
    if len(rets) >= 2 and len(downside) > 0 and np.std(downside) > 0:
        sortino = float(np.mean(rets) / np.std(downside) * math.sqrt(TRADING_DAYS))
    else:
        sortino = sharpe if sharpe > 0 else 0.0

    # drawdown on the equity path including start point
    path = np.array([initial_capital] + [v for _, v in equity_curve], dtype=float)
    peaks = np.maximum.accumulate(path)
    dd = (path - peaks) / peaks
    max_dd = float(-dd.min()) if len(dd) else 0.0
    # drawdown duration: longest run below previous peak
    duration = longest = 0
    for x, p in zip(path, peaks):
        if x < p:
            duration += 1
            longest = max(longest, duration)
        else:
            duration = 0
    calmar = float(cagr / max_dd) if max_dd > 1e-9 else (cagr / 1e-9 if cagr > 0 else 0.0)

    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
    win_rate = len(wins) / len(trades) if trades else 0.0
    expectancy = net / len(trades) if trades else 0.0

    if len(rets) >= 5:
        var_95 = float(np.percentile(rets, 5))
        tail = rets[rets <= var_95]
        cvar_95 = float(np.mean(tail)) if len(tail) else var_95
    else:
        var_95 = cvar_95 = 0.0

    turnover = (
        sum((t.entry_price + t.exit_price) * t.quantity for t in trades) / initial_capital
        if trades
        else 0.0
    )
    holding_minutes = sum(
        (t.exit_ts - t.entry_ts).total_seconds() / 60 for t in trades
    )
    session_minutes = n_days * 375
    time_in_market = holding_minutes / session_minutes if session_minutes > 0 else 0.0

    return Metrics(
        n_trades=len(trades),
        n_days=n_days,
        net_pnl=round(net, 2),
        total_return=total_return,
        cagr=cagr,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_drawdown=max_dd,
        max_drawdown_duration_days=longest,
        profit_factor=profit_factor if math.isfinite(profit_factor) else 999.0,
        win_rate=win_rate,
        expectancy=round(expectancy, 2),
        avg_win=round(float(np.mean(wins)), 2) if wins else 0.0,
        avg_loss=round(float(np.mean(losses)), 2) if losses else 0.0,
        cvar_95=cvar_95,
        var_95=var_95,
        turnover=round(turnover, 2),
        avg_mae=round(float(np.mean([t.mae for t in trades])), 2) if trades else 0.0,
        avg_mfe=round(float(np.mean([t.mfe for t in trades])), 2) if trades else 0.0,
        largest_loss=round(min((t.net_pnl for t in trades), default=0.0), 2),
        time_in_market_frac=round(time_in_market, 4),
    )
