"""Broker adapter protocol.

Everything the execution daemon needs, nothing more. Market-data access is a
separate protocol (qft.data.provider) — an execution adapter is not a data
vendor and vice versa.
"""

from __future__ import annotations

from typing import Protocol

from qft.domain.orders import ApprovedOrder, BrokerAck, Fill, OrderStatus
from qft.domain.portfolio import Position


class BrokerError(Exception):
    """Broker rejected or errored. The order did NOT go on book."""


class BrokerTimeout(Exception):
    """Ambiguous outcome: the order MAY be on book. Callers must reconcile via
    order_status_by_reference before any retry."""


class BrokerAdapter(Protocol):
    def place(self, order: ApprovedOrder) -> BrokerAck: ...

    def cancel(self, order_reference_id: str) -> OrderStatus: ...

    def order_status(self, broker_order_id: str) -> OrderStatus: ...

    def order_status_by_reference(self, order_reference_id: str) -> OrderStatus | None:
        """None means: the broker has no record of this reference id."""
        ...

    def trades_for_order(self, broker_order_id: str) -> list[Fill]: ...

    def positions(self) -> list[Position]: ...

    def available_margin(self) -> float: ...
