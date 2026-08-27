"""Execution daemon + simulated broker + ledger + reconciliation tests,
including the chaos scenarios that define production readiness."""

from __future__ import annotations

from datetime import timedelta

import pytest

from qft.brokers.simulated import SimulatedBroker, make_position
from qft.domain.enums import (
    Environment,
    Exchange,
    KillSwitch,
    OrderState,
    OrderType,
    Product,
    Segment,
    Side,
    Validity,
)
from qft.domain.orders import ApprovedOrder
from qft.domain.portfolio import LedgerEventType
from qft.execution.daemon import ExecutionDaemon
from qft.portfolio.ledger import Ledger
from qft.reconciliation.service import Reconciler
from qft.risk.kill_switch import KillSwitchManager
from tests.conftest import TRADING_TS, make_quote

pytestmark = pytest.mark.integration

SYMBOL = "NIFTY26SEP24500CE"


def make_order(ref: str = "ord_1", qty: int = 65, order_type: OrderType = OrderType.LIMIT,
               price: float | None = 85.0, side: Side = Side.BUY) -> ApprovedOrder:
    return ApprovedOrder(
        order_reference_id=ref,
        intent_id="int_1",
        decision_id="rd_1",
        ts=TRADING_TS,
        exchange=Exchange.NSE,
        segment=Segment.FNO,
        trading_symbol=SYMBOL,
        side=side,
        quantity=qty,
        order_type=order_type,
        product=Product.MIS,
        validity=Validity.DAY,
        price=price,
        expires_at=TRADING_TS + timedelta(seconds=30),
    )


@pytest.fixture
def broker() -> SimulatedBroker:
    b = SimulatedBroker()
    b.set_quote(SYMBOL, make_quote(f"NSE:FNO:{SYMBOL}", 85.0, bid=84.9, ask=85.0, oi=2e6))
    return b


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite", Environment.PAPER, initial_capital=50_000)


def test_happy_path_fill(broker, ledger) -> None:
    daemon = ExecutionDaemon(broker, ledger)
    status = daemon.submit(make_order(), now=TRADING_TS)
    assert status.state is OrderState.FILLED
    fills = ledger.fills()
    assert len(fills) == 1
    assert fills[0].quantity == 65
    assert fills[0].price == pytest.approx(85.0)  # filled at the ask, within limit
    # ledger event trail: ORDER_REQUEST before BROKER_ACK before FILL
    types = [e["type"] for e in ledger.events()]
    assert types.index("ORDER_REQUEST") < types.index("BROKER_ACK")
    assert "FILL" in types


def test_duplicate_submit_is_noop(broker, ledger) -> None:
    daemon = ExecutionDaemon(broker, ledger)
    daemon.submit(make_order(), now=TRADING_TS)
    daemon.submit(make_order(), now=TRADING_TS)  # same reference id
    assert len(ledger.fills()) == 1
    reqs = ledger.events(LedgerEventType.ORDER_REQUEST)
    assert len(reqs) == 1


def test_timeout_before_book_never_duplicates(broker, ledger) -> None:
    """Timeout where the order never reached the book: daemon must mark it
    rejected after reference lookup and must NOT have created a broker order."""
    broker.failures.timeout_before_book = 1
    daemon = ExecutionDaemon(broker, ledger)
    status = daemon.submit(make_order(), now=TRADING_TS)
    assert status.state is OrderState.REJECTED
    assert broker.order_status_by_reference("ord_1") is None
    assert ledger.fills() == []


def test_timeout_after_book_recovers_fill_without_resubmit(broker, ledger) -> None:
    """THE critical idempotency case: network timeout AFTER the broker accepted.
    The daemon must discover the live order via reference id and adopt its fill —
    and never place a second order."""
    broker.failures.timeout_after_book = 1
    daemon = ExecutionDaemon(broker, ledger)
    status = daemon.submit(make_order(), now=TRADING_TS)
    assert status.state is OrderState.FILLED
    assert status.filled_quantity == 65
    assert len(ledger.fills()) == 1
    # Exactly one order exists at the broker
    assert broker.order_status_by_reference("ord_1") is not None
    reqs = ledger.events(LedgerEventType.ORDER_REQUEST)
    assert len(reqs) == 1


def test_rejection_recorded(broker, ledger) -> None:
    broker.failures.reject_next = 1
    daemon = ExecutionDaemon(broker, ledger)
    status = daemon.submit(make_order(), now=TRADING_TS)
    assert status.state is OrderState.REJECTED
    assert ledger.fills() == []


def test_partial_fill_then_complete(broker, ledger) -> None:
    broker.failures.partial_fill_next = 1
    daemon = ExecutionDaemon(broker, ledger)
    status = daemon.submit(make_order(), now=TRADING_TS)
    assert status.state is OrderState.PARTIALLY_FILLED
    assert status.filled_quantity == 32
    # next tick completes the order
    broker.set_quote(SYMBOL, make_quote(f"NSE:FNO:{SYMBOL}", 85.0, bid=84.9, ask=85.0, oi=2e6))
    status2 = daemon.poll_order("ord_1")
    assert status2.state is OrderState.FILLED
    fills = ledger.fills()
    assert sum(f.quantity for f in fills) == 65
    assert len(fills) == 2


def test_expired_approved_order_refused(broker, ledger) -> None:
    daemon = ExecutionDaemon(broker, ledger)
    late = TRADING_TS + timedelta(seconds=60)
    status = daemon.submit(make_order(), now=late)
    assert status.state is OrderState.REJECTED
    assert broker.order_status_by_reference("ord_1") is None


def test_restart_adopts_existing_broker_order(broker, ledger) -> None:
    """Daemon restarts between submission and fill: the new daemon instance must
    adopt the broker's record instead of resubmitting."""
    daemon1 = ExecutionDaemon(broker, ledger)
    daemon1.submit(make_order(), now=TRADING_TS)
    # new daemon, same broker & ledger (fresh in-memory state = restart)
    daemon2 = ExecutionDaemon(broker, ledger)
    status = daemon2.submit(make_order(), now=TRADING_TS)
    assert status.state is OrderState.FILLED
    reqs = ledger.events(LedgerEventType.ORDER_REQUEST)
    assert len(reqs) == 1  # no second submission


def test_limit_not_marketable_rests_then_fills(broker, ledger) -> None:
    daemon = ExecutionDaemon(broker, ledger)
    status = daemon.submit(make_order(price=80.0), now=TRADING_TS)  # below ask -> rests
    assert status.state is OrderState.ACKED
    broker.set_quote(SYMBOL, make_quote(f"NSE:FNO:{SYMBOL}", 79.9, bid=79.8, ask=79.95, oi=2e6))
    status2 = daemon.poll_order("ord_1")
    assert status2.state is OrderState.FILLED
    assert ledger.fills()[0].price <= 80.0


def test_reconciliation_clean(broker, ledger) -> None:
    daemon = ExecutionDaemon(broker, ledger)
    daemon.submit(make_order(), now=TRADING_TS)
    ks = KillSwitchManager()
    result = Reconciler(broker, ledger, ks).run()
    assert result.reconciled
    assert ks.state is KillSwitch.NONE


def test_reconciliation_mismatch_trips_soft_kill(broker, ledger) -> None:
    """Unexpected broker position (e.g. manual trade in the app) must halt
    new trading."""
    daemon = ExecutionDaemon(broker, ledger)
    daemon.submit(make_order(), now=TRADING_TS)
    # inject an unexplained broker-side position
    broker._positions["BANKNIFTY26SEP52000CE"] = make_position(
        "BANKNIFTY26SEP52000CE", 30, 200.0, TRADING_TS
    )
    ks = KillSwitchManager()
    result = Reconciler(broker, ledger, ks).run()
    assert not result.reconciled
    assert ks.state is KillSwitch.SOFT
    assert any("BANKNIFTY" in m for m in result.mismatches)
    recon_events = ledger.events(LedgerEventType.RECONCILIATION)
    assert recon_events and recon_events[-1]["payload"]["reconciled"] is False


def test_ledger_positions_and_pnl_roundtrip(broker, ledger) -> None:
    daemon = ExecutionDaemon(broker, ledger)
    daemon.submit(make_order(), now=TRADING_TS)  # buy 65 @ 85.0
    # sell to close at 90
    broker.set_quote(SYMBOL, make_quote(f"NSE:FNO:{SYMBOL}", 90.0, bid=90.0, ask=90.1, oi=2e6))
    daemon.submit(make_order(ref="ord_2", side=Side.SELL, order_type=OrderType.LIMIT, price=90.0), now=TRADING_TS)
    positions = ledger.positions()
    assert positions[SYMBOL].is_flat
    pnl = positions[SYMBOL].realized_pnl
    assert pnl == pytest.approx((90.0 - 85.0) * 65, abs=1.0)
    view = ledger.portfolio_view(TRADING_TS + timedelta(minutes=5))
    assert view.equity == pytest.approx(50_000 + pnl, abs=1.0)
    assert view.open_position_count == 0
