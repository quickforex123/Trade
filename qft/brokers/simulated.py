"""Simulated broker for backtest, paper trading, and chaos tests.

Fills against supplied quotes with a configurable slippage/latency model and
first-class failure injection: timeouts before/after book placement, rejects,
partial fills. A timeout-after-placement leaves the order live at the broker
while the caller sees an exception — exactly the ambiguity the execution
daemon must handle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from qft.brokers.base import BrokerError, BrokerTimeout
from qft.domain.enums import OrderState, OrderType, Side
from qft.domain.ids import new_id
from qft.domain.market import Quote
from qft.domain.orders import ApprovedOrder, BrokerAck, Fill, OrderStatus
from qft.domain.portfolio import Position
from qft.domain.time import ensure_utc, now_utc

logger = logging.getLogger(__name__)


@dataclass
class FailurePlan:
    """Chaos-injection switches, consumed one order at a time."""

    timeout_before_book: int = 0  # raise timeout, order NOT on book
    timeout_after_book: int = 0  # raise timeout, order IS on book
    reject_next: int = 0
    partial_fill_next: int = 0  # fill half quantity, leave rest open


@dataclass
class _SimOrder:
    order: ApprovedOrder
    broker_order_id: str
    state: OrderState
    filled: int = 0
    avg_price: float | None = None
    fills: list[Fill] = field(default_factory=list)


class SimulatedBroker:
    def __init__(
        self,
        slippage_pct: float = 0.0005,
        fill_market_orders_immediately: bool = True,
    ) -> None:
        self._orders: dict[str, _SimOrder] = {}  # by reference id
        self._by_broker_id: dict[str, str] = {}
        self._quotes: dict[str, Quote] = {}
        self._positions: dict[str, Position] = {}
        self._margin = 1e12  # margin modelling is the risk engine's job in sim
        self._slippage = slippage_pct
        self._fill_immediate = fill_market_orders_immediately
        self.failures = FailurePlan()

    # -- market state supplied by the harness --------------------------------

    def set_quote(self, trading_symbol: str, quote: Quote) -> None:
        self._quotes[trading_symbol] = quote
        self._try_fill_open_orders(trading_symbol)

    def set_margin(self, margin: float) -> None:
        self._margin = margin

    # -- BrokerAdapter --------------------------------------------------------

    def place(self, order: ApprovedOrder) -> BrokerAck:
        if self.failures.timeout_before_book > 0:
            self.failures.timeout_before_book -= 1
            raise BrokerTimeout("simulated timeout before order reached book")

        if order.order_reference_id in self._orders:
            # Idempotency at the venue: same reference id returns the same order.
            existing = self._orders[order.order_reference_id]
            return BrokerAck(
                order_reference_id=order.order_reference_id,
                broker_order_id=existing.broker_order_id,
                ts=now_utc(),
                raw_status="DUPLICATE_REFERENCE",
            )

        if self.failures.reject_next > 0:
            self.failures.reject_next -= 1
            sim = _SimOrder(order, new_id("gw"), OrderState.REJECTED)
            self._register(sim)
            raise BrokerError("simulated rejection")

        sim = _SimOrder(order, new_id("gw"), OrderState.ACKED)
        self._register(sim)

        if self.failures.timeout_after_book > 0:
            self.failures.timeout_after_book -= 1
            self._maybe_fill(sim)
            raise BrokerTimeout("simulated timeout AFTER order reached book")

        self._maybe_fill(sim)
        return BrokerAck(
            order_reference_id=order.order_reference_id,
            broker_order_id=sim.broker_order_id,
            ts=now_utc(),
            raw_status=sim.state.value,
        )

    def cancel(self, order_reference_id: str) -> OrderStatus:
        sim = self._orders.get(order_reference_id)
        if sim is None:
            raise BrokerError(f"unknown order {order_reference_id}")
        if sim.state in (OrderState.ACKED, OrderState.PARTIALLY_FILLED):
            sim.state = OrderState.CANCELLED
        return self._status(sim)

    def order_status(self, broker_order_id: str) -> OrderStatus:
        ref = self._by_broker_id.get(broker_order_id)
        if ref is None:
            raise BrokerError(f"unknown broker order {broker_order_id}")
        return self._status(self._orders[ref])

    def order_status_by_reference(self, order_reference_id: str) -> OrderStatus | None:
        sim = self._orders.get(order_reference_id)
        return None if sim is None else self._status(sim)

    def trades_for_order(self, broker_order_id: str) -> list[Fill]:
        ref = self._by_broker_id.get(broker_order_id)
        if ref is None:
            return []
        return list(self._orders[ref].fills)

    def positions(self) -> list[Position]:
        return [p for p in self._positions.values() if not p.is_flat]

    def available_margin(self) -> float:
        return self._margin

    # -- internals ---------------------------------------------------------------

    def _register(self, sim: _SimOrder) -> None:
        self._orders[sim.order.order_reference_id] = sim
        self._by_broker_id[sim.broker_order_id] = sim.order.order_reference_id

    def _status(self, sim: _SimOrder) -> OrderStatus:
        return OrderStatus(
            order_reference_id=sim.order.order_reference_id,
            broker_order_id=sim.broker_order_id,
            state=sim.state,
            filled_quantity=sim.filled,
            pending_quantity=sim.order.quantity - sim.filled,
            average_fill_price=sim.avg_price,
            ts=now_utc(),
        )

    def _fill_price(self, order: ApprovedOrder, quote: Quote) -> float | None:
        """Marketable price with slippage, or None when not fillable."""
        if order.order_type == OrderType.MARKET:
            base = (quote.ask if order.side is Side.BUY else quote.bid) or quote.ltp
            if base <= 0:
                return None
            slip = base * self._slippage
            return base + slip if order.side is Side.BUY else base - slip
        assert order.price is not None
        if order.side is Side.BUY:
            ask = quote.ask or quote.ltp
            return min(order.price, ask) if 0 < ask <= order.price else None
        bid = quote.bid or quote.ltp
        return max(order.price, bid) if bid >= order.price > 0 else None

    def _maybe_fill(self, sim: _SimOrder) -> None:
        if sim.state not in (OrderState.ACKED, OrderState.PARTIALLY_FILLED):
            return
        quote = self._quotes.get(sim.order.trading_symbol)
        if quote is None:
            return
        if sim.order.order_type != OrderType.MARKET and not self._fill_immediate:
            return
        price = self._fill_price(sim.order, quote)
        if price is None:
            return
        remaining = sim.order.quantity - sim.filled
        qty = remaining
        if self.failures.partial_fill_next > 0:
            self.failures.partial_fill_next -= 1
            qty = max(1, remaining // 2)
        fill = Fill(
            fill_id=new_id("fill"),
            order_reference_id=sim.order.order_reference_id,
            broker_order_id=sim.broker_order_id,
            trading_symbol=sim.order.trading_symbol,
            side=sim.order.side,
            quantity=qty,
            price=round(price, 2),
            ts=now_utc(),
        )
        sim.fills.append(fill)
        prev_notional = (sim.avg_price or 0.0) * sim.filled
        sim.filled += qty
        sim.avg_price = (prev_notional + fill.price * qty) / sim.filled
        sim.state = OrderState.FILLED if sim.filled >= sim.order.quantity else OrderState.PARTIALLY_FILLED
        self._apply_fill_to_position(fill)

    def _try_fill_open_orders(self, trading_symbol: str) -> None:
        for sim in self._orders.values():
            if (
                sim.order.trading_symbol == trading_symbol
                and sim.state in (OrderState.ACKED, OrderState.PARTIALLY_FILLED)
            ):
                self._maybe_fill(sim)

    def _apply_fill_to_position(self, fill: Fill) -> None:
        pos = self._positions.get(fill.trading_symbol)
        signed = fill.quantity if fill.side is Side.BUY else -fill.quantity
        if pos is None or pos.net_quantity == 0:
            self._positions[fill.trading_symbol] = Position(
                instrument_key=fill.trading_symbol,
                trading_symbol=fill.trading_symbol,
                net_quantity=signed,
                average_price=fill.price,
                last_update=fill.ts,
            )
            return
        new_qty = pos.net_quantity + signed
        realized = pos.realized_pnl
        if pos.net_quantity * signed < 0:  # reducing / closing / flipping
            closed = min(abs(signed), abs(pos.net_quantity))
            direction = 1 if pos.net_quantity > 0 else -1
            realized += direction * (fill.price - pos.average_price) * closed
            avg = pos.average_price if new_qty * pos.net_quantity > 0 else fill.price
        else:  # adding
            avg = (pos.average_price * abs(pos.net_quantity) + fill.price * abs(signed)) / abs(new_qty)
        self._positions[fill.trading_symbol] = Position(
            instrument_key=pos.instrument_key,
            trading_symbol=pos.trading_symbol,
            net_quantity=new_qty,
            average_price=avg if new_qty != 0 else 0.0,
            realized_pnl=realized,
            last_update=fill.ts,
        )


def make_position(
    trading_symbol: str, net_quantity: int, average_price: float, ts: datetime
) -> Position:
    """Test helper for constructing broker-side positions."""
    return Position(
        instrument_key=trading_symbol,
        trading_symbol=trading_symbol,
        net_quantity=net_quantity,
        average_price=average_price,
        last_update=ensure_utc(ts),
    )
