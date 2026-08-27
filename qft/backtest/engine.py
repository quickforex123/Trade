"""Event-driven intraday backtest engine.

Honesty rules:
- Signals are computed strictly from bars closed BEFORE the decision time
  (the feature engine raises on leakage).
- Entries fill at the NEXT bar's open plus slippage, never at the signal bar.
- Stops/targets are evaluated conservatively inside each bar: if both stop and
  target lie within a bar's range, the STOP is assumed to hit first.
- Every trade pays the full Indian cost stack via CostModel.
- Sizing is the risk rule: lots = floor(risk_budget / (stop_points * lot_size)),
  capped by max_lots; 0 lots ⇒ no trade (min-lot rule respected).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from qft.costs.model import CostModel
from qft.domain.enums import Direction, Regime, Side
from qft.domain.ids import deterministic_id
from qft.domain.market import Bar
from qft.domain.portfolio import TradeRecord
from qft.domain.time import IST
from qft.features.engine import FeatureEngine
from qft.regime.engine import RegimeEngine
from qft.strategies.base import Strategy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 50_000.0
    risk_per_trade_pct: float = 0.015
    max_lots: int = 1
    lot_size: int = 65
    slippage_pct: float = 0.0003  # per side, on futures price
    entry_cutoff_ist: time = time(15, 0)
    square_off_ist: time = time(15, 10)
    max_daily_loss_pct: float = 0.03


@dataclass
class _OpenPosition:
    direction: Direction
    entry_price: float
    quantity: int
    entry_ts: datetime
    stop: float
    target: float
    time_exit: datetime
    signal_rationale: str
    regime: Regime
    setup_digest: str
    mfe: float = 0.0
    mae: float = 0.0


@dataclass
class BacktestResult:
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    signals_generated: int = 0
    signals_skipped_sizing: int = 0
    days: int = 0

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)


class BacktestEngine:
    """Runs one strategy over one instrument's intraday bars (grouped by day)."""

    def __init__(
        self,
        strategy: Strategy,
        feature_engine: FeatureEngine,
        regime_engine: RegimeEngine,
        cost_model: CostModel,
        config: BacktestConfig,
    ) -> None:
        self._strategy = strategy
        self._features = feature_engine
        self._regimes = regime_engine
        self._costs = cost_model
        self._cfg = config

    def run(self, bars_by_day: dict[str, list[Bar]], warmup_bars: int = 12) -> BacktestResult:
        result = BacktestResult()
        equity = self._cfg.initial_capital
        prev_close: float | None = None
        prev_high: float | None = None
        prev_low: float | None = None

        for day in sorted(bars_by_day):
            bars = sorted(bars_by_day[day], key=lambda b: b.ts)
            if len(bars) < warmup_bars + 2:
                continue
            result.days += 1
            self._regimes.reset()
            day_start_equity = equity
            daily_stop = day_start_equity * self._cfg.max_daily_loss_pct
            position: _OpenPosition | None = None
            underlying = self._strategy.spec.allowed_underlyings[0]

            for i in range(warmup_bars, len(bars)):
                bar = bars[i]
                ist = bar.ts.astimezone(IST)
                as_of = bar.ts  # decision at this bar's OPEN; features see bars < as_of

                # ---- manage open position against THIS bar's range ----
                if position is not None:
                    exit_price, reason = self._check_exit(position, bar, ist)
                    if exit_price is not None:
                        equity = self._close(result, position, exit_price, bar.ts, reason, equity)
                        position = None

                # ---- day-level loss circuit breaker ----
                if equity - day_start_equity <= -daily_stop:
                    if position is not None:
                        equity = self._close(result, position, bar.open, bar.ts, "KILL", equity)
                        position = None
                    break

                # ---- new entries ----
                if position is None and ist.time() <= self._cfg.entry_cutoff_ist:
                    history = bars[:i]
                    try:
                        frame = self._features.compute(
                            underlying, as_of, history,
                            prev_day_close=prev_close,
                            prev_day_high=prev_high,
                            prev_day_low=prev_low,
                        )
                    except ValueError:
                        continue
                    regime = self._regimes.classify(frame)
                    signal = self._strategy.generate(frame, regime, None)
                    if signal is None:
                        continue
                    result.signals_generated += 1
                    atr = frame.get("atr", 0.0)
                    if atr <= 0:
                        continue
                    stop_points = atr * self._strategy.spec.stop_loss_atr_multiple
                    target_points = atr * self._strategy.spec.target_atr_multiple
                    risk_budget = equity * self._cfg.risk_per_trade_pct
                    lots = int(risk_budget // (stop_points * self._cfg.lot_size))
                    lots = min(lots, self._cfg.max_lots)
                    if lots < 1:
                        # Minimum lot violates the risk budget: NO TRADE.
                        result.signals_skipped_sizing += 1
                        continue
                    qty = lots * self._cfg.lot_size
                    slip = bar.open * self._cfg.slippage_pct
                    entry = bar.open + slip if signal.direction is Direction.LONG else bar.open - slip
                    sign = 1.0 if signal.direction is Direction.LONG else -1.0
                    position = _OpenPosition(
                        direction=signal.direction,
                        entry_price=entry,
                        quantity=qty,
                        entry_ts=bar.ts,
                        stop=entry - sign * stop_points,
                        target=entry + sign * target_points,
                        time_exit=bar.ts + timedelta(minutes=self._strategy.spec.max_holding_minutes),
                        signal_rationale=signal.rationale,
                        regime=regime,
                        setup_digest=frame.digest,
                    )

            # ---- forced square-off at day end ----
            if position is not None:
                last = bars[-1]
                equity = self._close(result, position, last.close, last.ts, "SESSION_END", equity)
                position = None

            day_last = bars[-1]
            prev_close = day_last.close
            prev_high = max(b.high for b in bars)
            prev_low = min(b.low for b in bars)
            result.equity_curve.append((day_last.ts, round(equity, 2)))

        return result

    # -- helpers -------------------------------------------------------------

    def _check_exit(
        self, pos: _OpenPosition, bar: Bar, ist: datetime
    ) -> tuple[float | None, str]:
        slip = bar.close * self._cfg.slippage_pct
        long = pos.direction is Direction.LONG
        # track excursions (per-unit, favorable positive)
        fav = (bar.high - pos.entry_price) if long else (pos.entry_price - bar.low)
        adv = (pos.entry_price - bar.low) if long else (bar.high - pos.entry_price)
        pos.mfe = max(pos.mfe, fav)
        pos.mae = max(pos.mae, adv)

        stop_hit = bar.low <= pos.stop if long else bar.high >= pos.stop
        target_hit = bar.high >= pos.target if long else bar.low <= pos.target
        if stop_hit:  # conservative: stop first when both hit
            price = pos.stop - slip if long else pos.stop + slip
            return price, "STOP"
        if target_hit:
            price = pos.target - slip if long else pos.target + slip
            return price, "TARGET"
        if bar.ts >= pos.time_exit:
            return (bar.close - slip if long else bar.close + slip), "TIME"
        if ist.time() >= self._cfg.square_off_ist:
            return (bar.close - slip if long else bar.close + slip), "SESSION_END"
        return None, ""

    def _close(
        self,
        result: BacktestResult,
        pos: _OpenPosition,
        exit_price: float,
        ts: datetime,
        reason: str,
        equity: float,
    ) -> float:
        long = pos.direction is Direction.LONG
        gross = (exit_price - pos.entry_price) * pos.quantity * (1 if long else -1)
        entry_side = Side.BUY if long else Side.SELL
        exit_side = Side.SELL if long else Side.BUY
        costs = (
            self._costs.future_leg(entry_side, pos.entry_price, pos.quantity).total
            + self._costs.future_leg(exit_side, exit_price, pos.quantity).total
        )
        net = gross - costs
        trade = TradeRecord(
            trade_id=deterministic_id("bt", pos.entry_ts.isoformat(), ts.isoformat(), reason),
            intent_id="",
            strategy_id=self._strategy.spec.strategy_id,
            strategy_version=self._strategy.spec.version,
            instrument_key="BACKTEST_FUT",
            trading_symbol="BACKTEST_FUT",
            side=entry_side,
            quantity=pos.quantity,
            entry_ts=pos.entry_ts,
            exit_ts=ts,
            entry_price=round(pos.entry_price, 2),
            exit_price=round(exit_price, 2),
            gross_pnl=round(gross, 2),
            costs=round(costs, 2),
            net_pnl=round(net, 2),
            mfe=round(pos.mfe, 2),
            mae=round(pos.mae, 2),
            exit_reason=reason,
            regime=pos.regime.value,
            setup_digest=pos.setup_digest,
        )
        result.trades.append(trade)
        return equity + net
