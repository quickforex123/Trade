"""Execution-side contracts. Orders exist only downstream of a RiskDecision."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from qft.domain.enums import (
    Exchange,
    OrderState,
    OrderType,
    Product,
    Segment,
    Side,
    Validity,
)
from qft.domain.time import ensure_utc


class ApprovedOrder(BaseModel):
    """The only input the execution daemon accepts.

    Carries the ids of the intent and the approving decision so the daemon can
    re-verify lineage, plus everything needed to submit without consulting any
    upstream component.
    """

    model_config = ConfigDict(frozen=True)

    order_reference_id: str  # deterministic client id — the idempotency key
    intent_id: str
    decision_id: str
    ts: datetime
    exchange: Exchange
    segment: Segment
    trading_symbol: str
    side: Side
    quantity: int = Field(ge=1)
    order_type: OrderType
    product: Product
    validity: Validity
    price: float | None = None
    trigger_price: float | None = None
    expires_at: datetime  # daemon refuses to submit after this

    _norm_ts = field_validator("ts")(ensure_utc)
    _norm_exp = field_validator("expires_at")(ensure_utc)


class OrderRequest(BaseModel):
    """Daemon-internal record of one submission attempt."""

    model_config = ConfigDict(frozen=True)

    order_reference_id: str
    attempt: int = Field(ge=1)
    ts: datetime
    payload_digest: str

    _norm_ts = field_validator("ts")(ensure_utc)


class BrokerAck(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_reference_id: str
    broker_order_id: str
    ts: datetime
    raw_status: str = ""

    _norm_ts = field_validator("ts")(ensure_utc)


class OrderStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_reference_id: str
    broker_order_id: str | None = None
    state: OrderState
    filled_quantity: int = 0
    pending_quantity: int = 0
    average_fill_price: float | None = None
    ts: datetime
    detail: str = ""

    _norm_ts = field_validator("ts")(ensure_utc)


class Fill(BaseModel):
    model_config = ConfigDict(frozen=True)

    fill_id: str
    order_reference_id: str
    broker_order_id: str
    trading_symbol: str
    side: Side
    quantity: int = Field(ge=1)
    price: float = Field(gt=0.0)
    ts: datetime

    _norm_ts = field_validator("ts")(ensure_utc)


# Legal order-state transitions; the daemon enforces these.
ORDER_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.SUBMITTED}),
    OrderState.SUBMITTED: frozenset(
        {OrderState.ACKED, OrderState.REJECTED, OrderState.UNKNOWN}
    ),
    OrderState.ACKED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED, OrderState.UNKNOWN}
    ),
    OrderState.UNKNOWN: frozenset(
        {
            OrderState.ACKED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.CANCELLED: frozenset(),
}


def can_transition(current: OrderState, new: OrderState) -> bool:
    return new in ORDER_TRANSITIONS[current]
