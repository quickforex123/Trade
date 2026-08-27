"""Portfolio/ledger contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from qft.domain.enums import Side
from qft.domain.time import ensure_utc


class LedgerEventType(StrEnum):
    TRADE_INTENT = "TRADE_INTENT"
    RISK_DECISION = "RISK_DECISION"
    ORDER_APPROVED = "ORDER_APPROVED"
    ORDER_REQUEST = "ORDER_REQUEST"
    BROKER_ACK = "BROKER_ACK"
    ORDER_STATUS = "ORDER_STATUS"
    FILL = "FILL"
    POSITION = "POSITION"
    PNL = "PNL"
    FEES = "FEES"
    SNAPSHOT = "SNAPSHOT"
    RESEARCH_OPINION = "RESEARCH_OPINION"
    RISK_EVENT = "RISK_EVENT"
    KILL_SWITCH = "KILL_SWITCH"
    RECONCILIATION = "RECONCILIATION"
    ARMING = "ARMING"
    NOTE = "NOTE"


class Position(BaseModel):
    """Net position in one instrument, derived from fills."""

    model_config = ConfigDict(frozen=True)

    instrument_key: str
    trading_symbol: str
    net_quantity: int  # signed: + long, - short
    average_price: float
    realized_pnl: float = 0.0
    last_update: datetime

    _norm_ts = field_validator("last_update")(ensure_utc)

    @property
    def is_flat(self) -> bool:
        return self.net_quantity == 0


class TradeRecord(BaseModel):
    """One completed round-trip trade — the unit of post-trade attribution
    and of the trading memory."""

    model_config = ConfigDict(frozen=True)

    trade_id: str
    intent_id: str
    strategy_id: str
    strategy_version: str
    instrument_key: str
    trading_symbol: str
    side: Side  # entry side
    quantity: int
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    slippage_entry: float = 0.0  # vs. arrival/reference price, rupees per unit
    slippage_exit: float = 0.0
    mfe: float = 0.0  # max favorable excursion, rupees per unit
    mae: float = 0.0  # max adverse excursion, rupees per unit
    exit_reason: str = ""  # STOP | TARGET | TIME | SESSION_END | KILL | MANUAL
    regime: str = ""
    setup_digest: str = ""  # hash of the entry FeatureFrame
    ai_context: str = ""

    _norm_ets = field_validator("entry_ts")(ensure_utc)
    _norm_xts = field_validator("exit_ts")(ensure_utc)
