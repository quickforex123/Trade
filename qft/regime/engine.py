"""Regime engine: FeatureFrame -> Regime, with hysteresis.

Thresholds are configuration, not magic — they are explicit constructor
arguments so backtests can sweep them and so nothing here is hidden state.
Order of precedence: structural vetoes first (event risk, illiquidity,
expiry), then volatility, then direction.
"""

from __future__ import annotations

from datetime import date

from qft.domain.enums import Regime
from qft.features.engine import FeatureFrame


class RegimeEngine:
    def __init__(
        self,
        high_vol_atr_pct: float = 0.0035,
        low_vol_atr_pct: float = 0.0010,
        trend_strength_min: float = 0.35,
        trend_momentum_min: float = 0.0015,
        breakout_requires_or: bool = True,
        max_spread_pct: float = 0.0075,
        mean_revert_zscore: float = 1.5,
        hysteresis_bars: int = 2,
        event_dates: frozenset[str] = frozenset(),
        expiry_dates: frozenset[str] = frozenset(),
    ) -> None:
        self._hi_vol = high_vol_atr_pct
        self._lo_vol = low_vol_atr_pct
        self._ts_min = trend_strength_min
        self._mom_min = trend_momentum_min
        self._breakout_or = breakout_requires_or
        self._max_spread = max_spread_pct
        self._mr_z = mean_revert_zscore
        self._hysteresis = hysteresis_bars
        self._event_dates = event_dates
        self._expiry_dates = expiry_dates
        self._last: Regime | None = None
        self._pending: Regime | None = None
        self._pending_count = 0

    def classify(self, frame: FeatureFrame) -> Regime:
        raw = self._classify_raw(frame)
        return self._with_hysteresis(raw)

    # -- raw classification -------------------------------------------------

    def _classify_raw(self, f: FeatureFrame) -> Regime:
        d: date = f.as_of.date()
        if d.isoformat() in self._event_dates:
            return Regime.EVENT_RISK

        spread = f.get("chain_median_spread_pct")
        if spread == spread and spread > self._max_spread:  # NaN-safe
            return Regime.ILLIQUID

        if d.isoformat() in self._expiry_dates:
            return Regime.EXPIRY_REGIME

        atr_pct = f.get("atr_pct", 0.0)
        mom = f.get("mom_12", 0.0)
        trend = f.get("trend_strength", 0.0)
        z = f.get("zscore_20", 0.0)

        if atr_pct > self._hi_vol:
            return Regime.HIGH_VOLATILITY

        or_up = f.get("or_break_up", 0.0) > 0.5
        or_down = f.get("or_break_down", 0.0) > 0.5
        if (or_up or or_down) and trend >= self._ts_min and abs(mom) >= self._mom_min:
            return Regime.BREAKOUT

        if trend >= self._ts_min and mom >= self._mom_min:
            return Regime.TRENDING_UP
        if trend >= self._ts_min and mom <= -self._mom_min:
            return Regime.TRENDING_DOWN

        if abs(z) >= self._mr_z and trend < self._ts_min:
            return Regime.MEAN_REVERTING

        if atr_pct < self._lo_vol:
            return Regime.LOW_VOLATILITY

        return Regime.NO_TRADE

    # -- hysteresis ----------------------------------------------------------

    def _with_hysteresis(self, raw: Regime) -> Regime:
        # Structural vetoes apply immediately; direction changes need confirmation.
        immediate = {Regime.EVENT_RISK, Regime.ILLIQUID, Regime.EXPIRY_REGIME, Regime.NO_TRADE}
        if self._last is None or raw in immediate:
            self._last = raw
            self._pending = None
            self._pending_count = 0
            return raw
        if raw == self._last:
            self._pending = None
            self._pending_count = 0
            return raw
        if raw == self._pending:
            self._pending_count += 1
        else:
            self._pending = raw
            self._pending_count = 1
        if self._pending_count >= self._hysteresis:
            self._last = raw
            self._pending = None
            self._pending_count = 0
            return raw
        return self._last

    def reset(self) -> None:
        self._last = None
        self._pending = None
        self._pending_count = 0
