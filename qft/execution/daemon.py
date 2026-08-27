"""Execution daemon.

Receives ONLY ApprovedOrder objects (a TradeIntent that passed the risk
firewall). Responsibilities: validate lineage and expiry, persist intent to
submit BEFORE touching the network, submit exactly once per reference id,
resolve ambiguous outcomes via order-status-by-reference (never blind
retries), track the order state machine, record fills to the ledger.

Never assumes place_order == fill. Broker positions are the source of truth
after restart (see qft.reconciliation).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from qft.brokers.base import BrokerAdapter, BrokerError, BrokerTimeout
from qft.domain.enums import OrderState
from qft.domain.orders import ApprovedOrder, OrderStatus, can_transition
from qft.domain.portfolio import LedgerEventType
from qft.domain.time import ensure_utc, now_utc
from qft.portfolio.ledger import Ledger

logger = logging.getLogger(__name__)


class ExecutionDaemon:
    def __init__(self, broker: BrokerAdapter, ledger: Ledger) -> None:
        self._broker = broker
        self._ledger = ledger
        self._states: dict[str, OrderState] = {}
        self._broker_ids: dict[str, str] = {}
        self._recorded_fills: set[str] = set()

    # -- submission ------------------------------------------------------------

    def submit(self, order: ApprovedOrder, now: datetime | None = None) -> OrderStatus:
        now = ensure_utc(now) if now else now_utc()
        ref = order.order_reference_id

        # Idempotency: a reference id is submitted at most once per daemon life,
        # and a restart recovers state from the broker before resubmitting.
        if ref in self._states:
            logger.warning("duplicate submit for %s ignored (state=%s)", ref, self._states[ref])
            return self._current_status(ref)

        known = self._broker.order_status_by_reference(ref)
        if known is not None:
            logger.warning("order %s already known to broker (%s) — adopting, not resubmitting",
                           ref, known.state)
            self._adopt_status(known)
            return known

        if now >= order.expires_at:
            status = OrderStatus(
                order_reference_id=ref, state=OrderState.REJECTED, ts=now,
                detail="approved order expired before submission",
            )
            self._states[ref] = OrderState.REJECTED
            self._ledger.append(LedgerEventType.ORDER_STATUS, status.model_dump(mode="json"),
                                intent_id=order.intent_id, order_ref=ref)
            return status

        # Persist intent-to-submit BEFORE the network call.
        payload_digest = hashlib.sha256(
            order.model_dump_json().encode()
        ).hexdigest()[:16]
        self._states[ref] = OrderState.SUBMITTED
        self._ledger.append(
            LedgerEventType.ORDER_REQUEST,
            {"order": order.model_dump(mode="json"), "digest": payload_digest, "attempt": 1},
            intent_id=order.intent_id,
            order_ref=ref,
        )

        try:
            ack = self._broker.place(order)
        except BrokerTimeout:
            # Ambiguous: may or may not be on book. Mark UNKNOWN and reconcile.
            self._transition(ref, OrderState.UNKNOWN, order.intent_id,
                             detail="timeout during submission")
            return self.resolve_unknown(ref, order.intent_id)
        except BrokerError as e:
            self._transition(ref, OrderState.REJECTED, order.intent_id, detail=str(e))
            return self._current_status(ref)

        self._broker_ids[ref] = ack.broker_order_id
        self._ledger.append(LedgerEventType.BROKER_ACK, ack.model_dump(mode="json"),
                            intent_id=order.intent_id, order_ref=ref)
        self._transition(ref, OrderState.ACKED, order.intent_id)
        return self.poll_order(ref, order.intent_id)

    # -- reconciliation of ambiguous submissions ---------------------------------

    def resolve_unknown(self, ref: str, intent_id: str | None = None) -> OrderStatus:
        """After a timeout: ask the broker for our reference id. If the broker
        has no record, the order never reached the book and is safe to mark
        rejected. NEVER resubmit blindly."""
        status = self._broker.order_status_by_reference(ref)
        if status is None:
            self._transition(ref, OrderState.REJECTED, intent_id,
                             detail="broker has no record after timeout — not on book")
            return self._current_status(ref)
        self._adopt_status(status, intent_id)
        return status

    # -- polling ----------------------------------------------------------------

    def poll_order(self, ref: str, intent_id: str | None = None) -> OrderStatus:
        state = self._states.get(ref)
        if state is None:
            raise KeyError(f"unknown order {ref}")
        if state == OrderState.UNKNOWN:
            return self.resolve_unknown(ref, intent_id)
        broker_id = self._broker_ids.get(ref)
        if broker_id is None:
            return self._current_status(ref)
        status = self._broker.order_status(broker_id)
        self._adopt_status(status, intent_id)
        # Return the broker's view (it carries fill quantities) as long as our
        # state agrees; fall back to local state if a transition was refused.
        if self._states[ref] == status.state:
            return status
        return self._current_status(ref)

    def poll_open_orders(self) -> list[OrderStatus]:
        out = []
        for ref, state in list(self._states.items()):
            if state in (OrderState.SUBMITTED, OrderState.ACKED,
                         OrderState.PARTIALLY_FILLED, OrderState.UNKNOWN):
                out.append(self.poll_order(ref))
        return out

    def cancel(self, ref: str, intent_id: str | None = None) -> OrderStatus:
        status = self._broker.cancel(ref)
        self._adopt_status(status, intent_id)
        return self._current_status(ref)

    # -- internals -----------------------------------------------------------------

    def _adopt_status(self, status: OrderStatus, intent_id: str | None = None) -> None:
        ref = status.order_reference_id
        if status.broker_order_id:
            self._broker_ids[ref] = status.broker_order_id
        current = self._states.get(ref)
        if current is None:
            # Adoption after restart: the broker's record IS the truth — no
            # transition legality applies because we never held local state.
            self._states[ref] = status.state
            self._ledger.append(
                LedgerEventType.ORDER_STATUS,
                {"ref": ref, "from": "(adopted)", "to": status.state.value,
                 "detail": "adopted from broker after restart"},
                intent_id=intent_id,
                order_ref=ref,
            )
            self._record_new_fills(ref, intent_id)
            return
        if status.state != current:
            if can_transition(current, status.state):
                self._transition(ref, status.state, intent_id, detail=status.detail)
            elif current in (OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED):
                logger.debug("ignoring stale status %s for terminal order %s", status.state, ref)
            else:
                logger.error("ILLEGAL order transition %s -> %s for %s — keeping current, flagging",
                             current, status.state, ref)
                self._ledger.append(
                    LedgerEventType.RISK_EVENT,
                    {"kind": "illegal_order_transition", "ref": ref,
                     "from": current.value, "to": status.state.value},
                    order_ref=ref,
                )
        self._record_new_fills(ref, intent_id)

    def _record_new_fills(self, ref: str, intent_id: str | None) -> None:
        broker_id = self._broker_ids.get(ref)
        if broker_id is None:
            return
        for fill in self._broker.trades_for_order(broker_id):
            if fill.fill_id not in self._recorded_fills:
                self._recorded_fills.add(fill.fill_id)
                self._ledger.record_fill(fill, intent_id=intent_id)

    def _transition(self, ref: str, new_state: OrderState, intent_id: str | None,
                    detail: str = "") -> None:
        old = self._states.get(ref, OrderState.CREATED)
        if old != new_state and not can_transition(old, new_state):
            raise RuntimeError(f"illegal transition {old} -> {new_state} for {ref}")
        self._states[ref] = new_state
        self._ledger.append(
            LedgerEventType.ORDER_STATUS,
            {"ref": ref, "from": old.value, "to": new_state.value, "detail": detail},
            intent_id=intent_id,
            order_ref=ref,
        )
        logger.info("order %s: %s -> %s %s", ref, old, new_state, detail)

    def _current_status(self, ref: str) -> OrderStatus:
        return OrderStatus(
            order_reference_id=ref,
            broker_order_id=self._broker_ids.get(ref),
            state=self._states[ref],
            ts=now_utc(),
        )

    @property
    def order_states(self) -> dict[str, OrderState]:
        return dict(self._states)
