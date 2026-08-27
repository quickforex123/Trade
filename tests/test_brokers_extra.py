"""PaperBroker friction and Groww adapter security guards."""

from __future__ import annotations

from datetime import timedelta

import pytest

from qft.brokers.base import BrokerError
from qft.brokers.paper import PaperBroker
from qft.domain.enums import Exchange, OrderState, OrderType, Product, Segment, Side, Validity
from qft.domain.orders import ApprovedOrder
from tests.conftest import TRADING_TS, make_quote

pytestmark = pytest.mark.unit

SYMBOL = "NIFTY26SEP24500CE"


def _order(ref: str) -> ApprovedOrder:
    return ApprovedOrder(
        order_reference_id=ref,
        intent_id="int_1",
        decision_id="rd_1",
        ts=TRADING_TS,
        exchange=Exchange.NSE,
        segment=Segment.FNO,
        trading_symbol=SYMBOL,
        side=Side.BUY,
        quantity=65,
        order_type=OrderType.LIMIT,
        product=Product.MIS,
        validity=Validity.DAY,
        price=85.0,
        expires_at=TRADING_TS + timedelta(seconds=30),
    )


def test_paper_broker_injects_partial_fills_deterministically() -> None:
    broker = PaperBroker(partial_fill_prob=1.0)  # always partial
    broker.set_quote(SYMBOL, make_quote(f"NSE:FNO:{SYMBOL}", 85.0, bid=84.9, ask=85.0, oi=2e6))
    broker.place(_order("p1"))
    status = broker.order_status_by_reference("p1")
    assert status is not None
    assert status.state is OrderState.PARTIALLY_FILLED
    assert 0 < status.filled_quantity < 65


def test_paper_broker_zero_prob_fills_fully() -> None:
    broker = PaperBroker(partial_fill_prob=0.0)
    broker.set_quote(SYMBOL, make_quote(f"NSE:FNO:{SYMBOL}", 85.0, bid=84.9, ask=85.0, oi=2e6))
    broker.place(_order("p1"))
    status = broker.order_status_by_reference("p1")
    assert status.state is OrderState.FILLED


def test_groww_execution_adapter_refuses_outside_live(monkeypatch) -> None:
    """The production adapter must be unconstructible outside LIVE."""
    from qft.brokers.groww_execution import GrowwExecutionAdapter

    monkeypatch.delenv("QFT_ENVIRONMENT", raising=False)
    with pytest.raises(BrokerError, match="refuses to construct"):
        GrowwExecutionAdapter()
    monkeypatch.setenv("QFT_ENVIRONMENT", "PAPER")
    with pytest.raises(BrokerError, match="refuses to construct"):
        GrowwExecutionAdapter()


def test_groww_execution_adapter_requires_credentials(monkeypatch) -> None:
    from qft.brokers.groww_execution import GrowwExecutionAdapter

    monkeypatch.setenv("QFT_ENVIRONMENT", "LIVE")
    monkeypatch.delenv("GROWW_API_KEY", raising=False)
    monkeypatch.delenv("GROWW_API_SECRET", raising=False)
    monkeypatch.delenv("GROWW_API_TOTP", raising=False)
    with pytest.raises(BrokerError, match="GROWW_API_KEY"):
        GrowwExecutionAdapter()
