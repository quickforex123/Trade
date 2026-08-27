"""Risk firewall decision contract."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from qft.domain.time import ensure_utc


class RiskReason(StrEnum):
    """Exhaustive machine-readable reason codes for risk decisions."""

    APPROVED = "APPROVED"
    KILL_SWITCH_HARD = "KILL_SWITCH_HARD"
    KILL_SWITCH_SOFT = "KILL_SWITCH_SOFT"
    ENV_NOT_ARMED = "ENV_NOT_ARMED"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    DUPLICATE_INTENT = "DUPLICATE_INTENT"
    DUPLICATE_ORDER_IN_FLIGHT = "DUPLICATE_ORDER_IN_FLIGHT"
    MARKET_CLOSED = "MARKET_CLOSED"
    OUTSIDE_SESSION_WINDOW = "OUTSIDE_SESSION_WINDOW"
    EVENT_BLACKOUT = "EVENT_BLACKOUT"
    SNAPSHOT_MISSING = "SNAPSHOT_MISSING"
    SNAPSHOT_UNVERIFIED = "SNAPSHOT_UNVERIFIED"
    SNAPSHOT_STALE = "SNAPSHOT_STALE"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
    INSTRUMENT_NOT_ALLOWED = "INSTRUMENT_NOT_ALLOWED"
    EXPIRY_NOT_ALLOWED = "EXPIRY_NOT_ALLOWED"
    LOT_SIZE_INVALID = "LOT_SIZE_INVALID"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    LIQUIDITY_INSUFFICIENT = "LIQUIDITY_INSUFFICIENT"
    OI_TOO_LOW = "OI_TOO_LOW"
    INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    PREMIUM_CAP_EXCEEDED = "PREMIUM_CAP_EXCEEDED"
    PER_TRADE_LOSS_EXCEEDED = "PER_TRADE_LOSS_EXCEEDED"
    MIN_LOT_EXCEEDS_RISK_BUDGET = "MIN_LOT_EXCEEDS_RISK_BUDGET"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    WEEKLY_LOSS_LIMIT = "WEEKLY_LOSS_LIMIT"
    DRAWDOWN_HALT = "DRAWDOWN_HALT"
    MAX_CONCURRENT_POSITIONS = "MAX_CONCURRENT_POSITIONS"
    STRATEGY_EXPOSURE_LIMIT = "STRATEGY_EXPOSURE_LIMIT"
    UNDERLYING_EXPOSURE_LIMIT = "UNDERLYING_EXPOSURE_LIMIT"
    STRATEGY_DISABLED = "STRATEGY_DISABLED"
    ORDER_FREQUENCY_LIMIT = "ORDER_FREQUENCY_LIMIT"
    DAILY_ORDER_LIMIT = "DAILY_ORDER_LIMIT"
    BROKER_DEGRADED = "BROKER_DEGRADED"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    NAKED_SHORT_OPTION_FORBIDDEN = "NAKED_SHORT_OPTION_FORBIDDEN"
    REWARD_RISK_TOO_LOW = "REWARD_RISK_TOO_LOW"


class RiskDecision(BaseModel):
    """Outcome of the firewall for one TradeIntent. Approvals are logged too."""

    model_config = ConfigDict(frozen=True)

    decision_id: str
    intent_id: str
    ts: datetime
    approved: bool
    reasons: tuple[RiskReason, ...]
    evaluated_rules: tuple[str, ...] = ()
    snapshot_id: str = ""
    detail: str = ""

    _norm_ts = field_validator("ts")(ensure_utc)

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        if self.approved and any(r is not RiskReason.APPROVED for r in self.reasons):
            raise ValueError("approved decision must carry only APPROVED reason")
        if not self.approved and (
            not self.reasons or any(r is RiskReason.APPROVED for r in self.reasons)
        ):
            raise ValueError("rejection must carry at least one non-APPROVED reason")
