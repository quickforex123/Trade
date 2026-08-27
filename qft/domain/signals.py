"""Strategy signals and trade intents.

A Signal is a strategy's raw output. A TradeIntent is the fully-specified,
fusion-approved proposal handed to the risk firewall. Neither is an order.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qft.domain.enums import Direction, OptionType, OrderType, Regime, Side
from qft.domain.time import ensure_utc


class Signal(BaseModel):
    model_config = ConfigDict(frozen=True)

    signal_id: str
    strategy_id: str
    strategy_version: str
    ts: datetime
    underlying: str
    direction: Direction
    strength: float = Field(ge=0.0, le=1.0)
    regime: Regime
    features_digest: str = ""  # hash of the FeatureFrame the signal was computed from
    rationale: str = ""  # short deterministic reason string (rule ids, not prose)

    _norm_ts = field_validator("ts")(ensure_utc)


class TradeIntent(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent_id: str
    strategy_id: str
    strategy_version: str
    ts: datetime
    signal_expiry: datetime
    underlying: str
    instrument_key: str
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    side: Side
    lots: int = Field(ge=1)
    quantity: int = Field(ge=1)  # lots * lot_size, precomputed for auditability
    entry_type: OrderType
    entry_price_limit: float | None = None  # required for LIMIT
    max_slippage_pct: float = Field(ge=0.0, le=0.1)
    stop_condition: str  # deterministic rule expression, e.g. "premium <= 71.25"
    stop_loss_points: float = Field(gt=0)
    target_condition: str = ""
    time_exit_utc: datetime | None = None
    estimated_transaction_cost: float = Field(ge=0.0)
    estimated_max_loss: float = Field(gt=0.0)
    expected_reward: float = Field(ge=0.0)
    quant_confidence: float = Field(ge=0.0, le=1.0)
    ai_research_context: str = ""  # opinion id + adjustment applied, or "none"
    market_regime: Regime
    snapshot_id: str
    reason_code: str

    _norm_ts = field_validator("ts")(ensure_utc)
    _norm_exp = field_validator("signal_expiry")(ensure_utc)

    @field_validator("time_exit_utc")
    @classmethod
    def _norm_texit(cls, v: datetime | None) -> datetime | None:
        return None if v is None else ensure_utc(v)

    @model_validator(mode="after")
    def _coherent(self) -> TradeIntent:
        if self.entry_type == OrderType.LIMIT and (
            self.entry_price_limit is None or self.entry_price_limit <= 0
        ):
            raise ValueError("LIMIT intent requires a positive entry_price_limit")
        if self.signal_expiry <= self.ts:
            raise ValueError("signal_expiry must be after intent ts")
        if self.option_type is not None and (self.strike is None or self.expiry is None):
            raise ValueError("option intent requires strike and expiry")
        return self

    @property
    def expected_reward_risk(self) -> float:
        return self.expected_reward / self.estimated_max_loss
