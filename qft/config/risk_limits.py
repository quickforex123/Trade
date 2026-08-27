"""Risk firewall limits — the single schema for config/risk.yaml.

Values are validated at startup; an invalid file refuses to load (the process
must not start with a permissive fallback). No code path outside human-edited
YAML + restart can change these at runtime.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SessionWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_open_ist: time = time(9, 20)
    entry_close_ist: time = time(15, 0)
    square_off_ist: time = time(15, 10)

    @model_validator(mode="after")
    def _ordered(self) -> SessionWindow:
        if not (self.entry_open_ist < self.entry_close_ist <= self.square_off_ist):
            raise ValueError("session window must satisfy open < entry_close <= square_off")
        return self


class RiskLimits(BaseModel):
    """All monetary values in INR. Fractions are of current equity."""

    model_config = ConfigDict(frozen=True)

    initial_capital: float = Field(gt=0)

    max_capital_at_risk_per_trade_pct: float = Field(gt=0, le=0.05, default=0.015)
    max_premium_per_trade: float = Field(gt=0, default=6000)
    max_daily_loss_pct: float = Field(gt=0, le=0.10, default=0.03)
    max_weekly_loss_pct: float = Field(gt=0, le=0.20, default=0.06)
    max_drawdown_halt_pct: float = Field(gt=0, le=0.30, default=0.10)

    max_concurrent_positions: int = Field(ge=1, default=1)
    max_lots_per_order: int = Field(ge=1, default=1)
    max_orders_per_day: int = Field(ge=1, default=6)
    max_orders_per_minute: int = Field(ge=1, le=30, default=2)

    instrument_allowlist: tuple[str, ...] = ("NIFTY",)
    allowed_weekly_expiries: int = Field(ge=1, le=4, default=2)
    allow_monthly_expiry: bool = True

    max_spread_pct_options: float = Field(gt=0, le=0.05, default=0.0075)
    max_spread_pct_futures: float = Field(gt=0, le=0.01, default=0.0005)
    min_open_interest: float = Field(ge=0, default=1_500_000)
    min_reward_risk: float = Field(ge=0, default=1.2)
    max_slippage_pct: float = Field(gt=0, le=0.05, default=0.015)

    signal_ttl_seconds: float = Field(gt=0, le=300, default=20)
    max_tick_age_seconds: float = Field(gt=0, le=60, default=3)
    max_snapshot_age_seconds: float = Field(gt=0, le=600, default=90)

    session: SessionWindow = SessionWindow()
    event_blackout_dates: tuple[str, ...] = ()  # ISO dates: RBI policy, budget, etc.

    allow_short_options: bool = False  # structural: naked shorts forbidden
    require_research_opinion: bool = False

    @property
    def max_capital_at_risk_per_trade(self) -> float:
        return self.initial_capital * self.max_capital_at_risk_per_trade_pct

    def max_daily_loss(self, equity: float) -> float:
        return equity * self.max_daily_loss_pct

    def max_weekly_loss(self, equity: float) -> float:
        return equity * self.max_weekly_loss_pct


def load_risk_limits(path: Path | str) -> RiskLimits:
    """Load and validate limits. Any error is fatal by design."""
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"risk config {p} must be a mapping")
    return RiskLimits.model_validate(raw)
