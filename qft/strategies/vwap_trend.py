"""VWAP trend-continuation — second research strategy family.

Hypothesis: in a confirmed intraday trend, pullbacks toward session VWAP that
hold offer continuation entries in the trend direction.
"""

from __future__ import annotations

from qft.domain.enums import Direction, Regime
from qft.domain.ids import deterministic_id
from qft.domain.market import VerifiedMarketSnapshot
from qft.domain.signals import Signal
from qft.features.engine import FeatureFrame
from qft.strategies.base import Strategy, StrategySpec


class VwapTrendContinuation(Strategy):
    def __init__(
        self,
        max_vwap_dist: float = 0.0015,
        min_momentum: float = 0.0015,
        min_minutes: float = 45.0,
        max_minutes: float = 240.0,
    ) -> None:
        self.spec = StrategySpec(
            strategy_id="vwap_trend_v1",
            version="1.0.0",
            description="Pullback-to-VWAP continuation in established intraday trend",
            allowed_regimes=(Regime.TRENDING_UP, Regime.TRENDING_DOWN),
            max_holding_minutes=90,
            stop_loss_atr_multiple=1.2,
            target_atr_multiple=2.0,
            cooldown_minutes=45,
        )
        self._max_dist = max_vwap_dist
        self._min_mom = min_momentum
        self._min_minutes = min_minutes
        self._max_minutes = max_minutes

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

        vwap_dist = frame.get("vwap_dist")
        mom = frame.get("mom_36", frame.get("mom_12", 0.0))
        if vwap_dist != vwap_dist:  # NaN
            return None

        near_vwap = abs(vwap_dist) <= self._max_dist
        if not near_vwap:
            return None

        if regime is Regime.TRENDING_UP and mom >= self._min_mom and vwap_dist >= 0:
            direction = Direction.LONG
        elif regime is Regime.TRENDING_DOWN and mom <= -self._min_mom and vwap_dist <= 0:
            direction = Direction.SHORT
        else:
            return None

        strength = min(1.0, abs(mom) / (self._min_mom * 3))
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
            rationale=f"VWAP_PULLBACK dist={vwap_dist:.4f} mom36={mom:.4f}",
        )
