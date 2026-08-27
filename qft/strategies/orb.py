"""Opening-range breakout (ORB) — first research strategy family.

Hypothesis: a decisive break of the first-15-minute range with trend
confirmation continues in the break direction intraday. Entry via the
underlying's direction; instrument selection (which option/future to use)
belongs to the fusion/intent layer, not here.

This is a RESEARCH strategy: it earns production eligibility only through
the backtest survival gates and walk-forward scoring (qft.backtest).
"""

from __future__ import annotations

from qft.domain.enums import Direction, Regime
from qft.domain.ids import deterministic_id
from qft.domain.market import VerifiedMarketSnapshot
from qft.domain.signals import Signal
from qft.features.engine import FeatureFrame
from qft.strategies.base import Strategy, StrategySpec


class OpeningRangeBreakout(Strategy):
    def __init__(
        self,
        min_trend_strength: float = 0.35,
        min_minutes: float = 20.0,
        max_minutes: float = 180.0,
        min_or_range_atr: float = 0.5,
        max_or_range_atr: float = 3.0,
    ) -> None:
        self.spec = StrategySpec(
            strategy_id="orb_v1",
            version="1.0.0",
            description="15-minute opening range breakout with trend confirmation",
            allowed_regimes=(Regime.BREAKOUT, Regime.TRENDING_UP, Regime.TRENDING_DOWN),
            max_holding_minutes=120,
            stop_loss_atr_multiple=1.0,
            target_atr_multiple=2.0,
        )
        self._min_ts = min_trend_strength
        self._min_minutes = min_minutes
        self._max_minutes = max_minutes
        self._min_or_atr = min_or_range_atr
        self._max_or_atr = max_or_range_atr

    def generate(
        self,
        frame: FeatureFrame,
        regime: Regime,
        snapshot: VerifiedMarketSnapshot | None,
    ) -> Signal | None:
        if not self.permitted(regime, frame.underlying):
            return None
        minutes = frame.get("minutes_since_open", 0.0)
        if not (self._min_minutes <= minutes <= self._max_minutes):
            return None

        atr = frame.get("atr", 0.0)
        or_high = frame.get("or_high", 0.0)
        or_low = frame.get("or_low", 0.0)
        if atr <= 0 or or_high <= or_low:
            return None
        or_range_atr = (or_high - or_low) / atr
        # A degenerate or hyper-wide opening range invalidates the setup.
        if not (self._min_or_atr <= or_range_atr <= self._max_or_atr):
            return None

        trend = frame.get("trend_strength", 0.0)
        if trend < self._min_ts:
            return None

        up = frame.get("or_break_up", 0.0) > 0.5
        down = frame.get("or_break_down", 0.0) > 0.5
        if up == down:  # neither or both (impossible) — no trade
            return None
        vwap_dist = frame.get("vwap_dist", 0.0)
        if up and vwap_dist <= 0:
            return None  # break without VWAP confirmation
        if down and vwap_dist >= 0:
            return None

        direction = Direction.LONG if up else Direction.SHORT
        strength = min(1.0, trend * min(or_range_atr / self._min_or_atr, 2.0) / 2.0)
        return Signal(
            signal_id=deterministic_id(
                "sig", self.spec.strategy_id, frame.underlying, frame.as_of.isoformat()
            ),
            strategy_id=self.spec.strategy_id,
            strategy_version=self.spec.version,
            ts=frame.as_of,
            underlying=frame.underlying,
            direction=direction,
            strength=round(strength, 4),
            regime=regime,
            features_digest=frame.digest,
            rationale=f"ORB_{'UP' if up else 'DOWN'} or_range_atr={or_range_atr:.2f} trend={trend:.2f}",
        )
