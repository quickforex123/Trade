"""Deterministic, timestamped feature computation.

Pure functions of point-in-time bars plus (optionally) a verified snapshot's
chain. Every FeatureFrame records the input window bounds so leakage audits
are mechanical. No LLM, no network, no wall-clock reads.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime

import numpy as np
from pydantic import BaseModel, ConfigDict, field_validator

from qft.domain.market import Bar, OptionChain
from qft.domain.time import IST, ensure_utc


class FeatureFrame(BaseModel):
    """Feature values for one underlying at one instant."""

    model_config = ConfigDict(frozen=True)

    underlying: str
    as_of: datetime
    input_start: datetime
    input_end: datetime
    features: dict[str, float]

    _n1 = field_validator("as_of")(ensure_utc)
    _n2 = field_validator("input_start")(ensure_utc)
    _n3 = field_validator("input_end")(ensure_utc)

    @property
    def digest(self) -> str:
        items = "|".join(f"{k}={self.features[k]:.8g}" for k in sorted(self.features))
        basis = f"{self.underlying}|{self.as_of.isoformat()}|{items}"
        return hashlib.sha256(basis.encode()).hexdigest()[:16]

    def get(self, name: str, default: float = math.nan) -> float:
        return self.features.get(name, default)


def _returns(closes: np.ndarray) -> np.ndarray:
    return np.diff(closes) / closes[:-1]


def _true_range(h: np.ndarray, low: np.ndarray, c: np.ndarray) -> np.ndarray:
    prev_close = np.concatenate([[c[0]], c[:-1]])
    return np.maximum.reduce([h - low, np.abs(h - prev_close), np.abs(low - prev_close)])


class FeatureEngine:
    """Computes intraday features from 5m (or finer) bars of the underlying
    future/spot, plus chain-derived features when a chain is provided."""

    def __init__(self, atr_window: int = 14, momentum_windows: tuple[int, ...] = (6, 12, 36)):
        self._atr_window = atr_window
        self._momentum_windows = momentum_windows

    def compute(
        self,
        underlying: str,
        as_of: datetime,
        bars: list[Bar],
        chain: OptionChain | None = None,
        prev_day_close: float | None = None,
        prev_day_high: float | None = None,
        prev_day_low: float | None = None,
    ) -> FeatureFrame:
        as_of = ensure_utc(as_of)
        if not bars:
            raise ValueError("no bars supplied")
        for b in bars:
            if b.ts >= as_of:
                raise ValueError("bar at/after as_of leaked into feature inputs")
        bars = sorted(bars, key=lambda b: b.ts)

        o = np.array([b.open for b in bars])
        h = np.array([b.high for b in bars])
        low = np.array([b.low for b in bars])
        c = np.array([b.close for b in bars])
        v = np.array([b.volume for b in bars])

        f: dict[str, float] = {}
        last = float(c[-1])
        f["last_price"] = last

        # --- momentum over configured windows (fraction) ---
        for w in self._momentum_windows:
            if len(c) > w:
                f[f"mom_{w}"] = float(c[-1] / c[-1 - w] - 1.0)

        # --- session VWAP (bars are assumed to start at session open) ---
        typical = (h + low + c) / 3.0
        cum_v = float(np.sum(v))
        if cum_v > 0:
            vwap = float(np.sum(typical * v) / cum_v)
            f["vwap"] = vwap
            f["vwap_dist"] = (last - vwap) / vwap if vwap > 0 else 0.0

        # --- ATR & realized vol ---
        tr = _true_range(h, low, c)
        w = min(self._atr_window, len(tr))
        atr = float(np.mean(tr[-w:]))
        f["atr"] = atr
        f["atr_pct"] = atr / last if last > 0 else 0.0
        rets = _returns(c)
        if len(rets) >= 6:
            # annualized realized vol from bar returns (75 five-minute bars/day, 252 days)
            bars_per_day = 75
            f["rv_annual"] = float(np.std(rets, ddof=1) * math.sqrt(bars_per_day * 252))

        # --- trend strength: |sum of returns| / sum of |returns| ---
        if len(rets) >= 6:
            denom = float(np.sum(np.abs(rets)))
            f["trend_strength"] = float(abs(np.sum(rets)) / denom) if denom > 0 else 0.0

        # --- mean-reversion state: z-score of price vs. rolling mean ---
        if len(c) >= 20:
            window = c[-20:]
            sd = float(np.std(window, ddof=1))
            f["zscore_20"] = float((last - np.mean(window)) / sd) if sd > 0 else 0.0

        # --- opening range (first 3 bars = 15 minutes on 5m bars) ---
        n_or = min(3, len(bars))
        or_high = float(np.max(h[:n_or]))
        or_low = float(np.min(low[:n_or]))
        f["or_high"] = or_high
        f["or_low"] = or_low
        f["or_break_up"] = 1.0 if last > or_high else 0.0
        f["or_break_down"] = 1.0 if last < or_low else 0.0

        # --- gap vs. previous close ---
        if prev_day_close and prev_day_close > 0:
            f["gap_pct"] = float(o[0] / prev_day_close - 1.0)
        if prev_day_high and prev_day_high > 0:
            f["above_prev_high"] = 1.0 if last > prev_day_high else 0.0
        if prev_day_low and prev_day_low > 0:
            f["below_prev_low"] = 1.0 if last < prev_day_low else 0.0

        # --- session time (IST minutes since open) ---
        ist = as_of.astimezone(IST)
        f["minutes_since_open"] = float((ist.hour - 9) * 60 + ist.minute - 15)

        # --- chain-derived features ---
        if chain is not None and chain.rows:
            self._chain_features(f, chain)

        return FeatureFrame(
            underlying=underlying,
            as_of=as_of,
            input_start=bars[0].ts,
            input_end=bars[-1].ts,
            features=f,
        )

    @staticmethod
    def _chain_features(f: dict[str, float], chain: OptionChain) -> None:
        spot = chain.underlying_price
        calls = [r for r in chain.rows if r.instrument.option_type is not None
                 and r.instrument.option_type.value == "CE"]
        puts = [r for r in chain.rows if r.instrument.option_type is not None
                and r.instrument.option_type.value == "PE"]

        call_oi = sum(r.oi or 0.0 for r in calls)
        put_oi = sum(r.oi or 0.0 for r in puts)
        if call_oi > 0:
            f["pcr_oi"] = put_oi / call_oi
        call_vol = sum(r.quote.volume or 0.0 for r in calls)
        put_vol = sum(r.quote.volume or 0.0 for r in puts)
        if call_vol > 0:
            f["pcr_volume"] = put_vol / call_vol
        f["chain_call_oi"] = call_oi
        f["chain_put_oi"] = put_oi
        f["chain_call_doi"] = sum(r.oi_change or 0.0 for r in calls)
        f["chain_put_doi"] = sum(r.oi_change or 0.0 for r in puts)

        # ATM IV and 25-delta-ish skew proxy
        def _nearest(rows: list, target_strike: float):
            with_strike = [r for r in rows if r.instrument.strike]
            if not with_strike:
                return None
            return min(with_strike, key=lambda r: abs(r.instrument.strike - target_strike))

        atm_call = _nearest(calls, spot)
        atm_put = _nearest(puts, spot)
        ivs = [r.iv for r in (atm_call, atm_put) if r is not None and r.iv]
        if ivs:
            f["atm_iv"] = float(sum(ivs) / len(ivs))
        otm_put = _nearest(puts, spot * 0.98)
        otm_call = _nearest(calls, spot * 1.02)
        if otm_put is not None and otm_call is not None and otm_put.iv and otm_call.iv:
            f["skew_2pct"] = float(otm_put.iv - otm_call.iv)

        # liquidity: median spread of the 5 strikes nearest ATM
        near = sorted(
            (r for r in chain.rows if r.instrument.strike and r.quote.spread_pct is not None),
            key=lambda r: abs(r.instrument.strike - spot),
        )[:10]
        spreads = sorted(r.quote.spread_pct for r in near if r.quote.spread_pct is not None)
        if spreads:
            f["chain_median_spread_pct"] = float(spreads[len(spreads) // 2])
