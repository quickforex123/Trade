"""Groww READ-ONLY account adapter: positions, margins, order status.

Used by SHADOW mode and the reconciler when running against the real broker
WITHOUT any ability to place orders — the class simply has no order methods,
and the token it is given should be the same market/read scope token as the
data adapter, never the approval secret.

Endpoints follow docs/GROWW_API_REFERENCE.md (growwapi==1.5.0).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from qft.domain.enums import OrderState, Side
from qft.domain.orders import Fill, OrderStatus
from qft.domain.portfolio import Position

logger = logging.getLogger(__name__)

_BASE = "https://api.groww.in/v1"

# Groww order status vocabulary -> our OrderState (verified against docs; any
# unknown value maps to UNKNOWN, which the daemon treats as needs-reconciliation).
_STATUS_MAP = {
    "NEW": OrderState.ACKED,
    "ACKED": OrderState.ACKED,
    "OPEN": OrderState.ACKED,
    "TRIGGER_PENDING": OrderState.ACKED,
    "APPROVED": OrderState.ACKED,
    "EXECUTED": OrderState.FILLED,
    "COMPLETED": OrderState.FILLED,
    "PARTIALLY_EXECUTED": OrderState.PARTIALLY_FILLED,
    "REJECTED": OrderState.REJECTED,
    "CANCELLED": OrderState.CANCELLED,
    "FAILED": OrderState.REJECTED,
}


class GrowwReadOnlyAdapter:
    """Account state reader. Deliberately implements NO order placement."""

    def __init__(self, access_token: str, client: httpx.Client | None = None) -> None:
        if not access_token:
            raise ValueError("access token required")
        self._token = access_token
        self._client = client or httpx.Client(timeout=10.0)

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        resp = self._client.get(
            _BASE + path,
            params=params,
            headers={
                "x-request-id": str(uuid.uuid4()),
                "Authorization": f"Bearer {self._token}",
                "x-api-version": "1.0",
            },
        )
        if resp.status_code >= 400:
            raise ConnectionError(f"Groww HTTP {resp.status_code} on {path}")
        body = resp.json()
        if isinstance(body, dict) and body.get("status") == "FAILURE":
            err = body.get("error") or {}
            raise ConnectionError(f"Groww failure on {path}: {err.get('code')}")
        payload = body.get("payload", body) if isinstance(body, dict) else body
        return payload if isinstance(payload, dict) else {"data": payload}

    def positions(self, segment: str = "FNO") -> list[Position]:
        payload = self._get("/positions/user", params={"segment": segment})
        rows = payload.get("positions") or payload.get("data") or []
        out: list[Position] = []
        now = datetime.now(UTC)
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                buy_qty = int(r.get("quantity_buy") or r.get("buy_quantity") or 0)
                sell_qty = int(r.get("quantity_sell") or r.get("sell_quantity") or 0)
                net = int(r.get("net_quantity") or (buy_qty - sell_qty))
                symbol = str(r.get("trading_symbol") or "")
                if not symbol:
                    continue
                avg = float(r.get("net_price") or r.get("average_price") or 0.0)
                out.append(
                    Position(
                        instrument_key=symbol,
                        trading_symbol=symbol,
                        net_quantity=net,
                        average_price=avg,
                        last_update=now,
                    )
                )
            except (TypeError, ValueError) as e:
                logger.warning("unparsable broker position row: %s", e)
        return [p for p in out if not p.is_flat]

    def available_margin(self) -> float:
        payload = self._get("/margins/detail/user")
        for key in ("clear_cash", "net_margin_available", "available_margin", "cash"):
            if payload.get(key) is not None:
                return float(payload[key])
        raise ConnectionError("margin payload missing known fields — refusing to guess")

    def order_status_by_reference(self, order_reference_id: str, segment: str = "FNO") -> OrderStatus | None:
        try:
            payload = self._get(
                f"/order/status/reference/{order_reference_id}", params={"segment": segment}
            )
        except ConnectionError as e:
            if "404" in str(e):
                return None
            raise
        raw = str(payload.get("order_status") or payload.get("status") or "").upper()
        return OrderStatus(
            order_reference_id=order_reference_id,
            broker_order_id=payload.get("groww_order_id"),
            state=_STATUS_MAP.get(raw, OrderState.UNKNOWN),
            filled_quantity=int(payload.get("filled_quantity") or 0),
            pending_quantity=int(payload.get("pending_quantity") or 0),
            average_fill_price=(
                float(payload["average_fill_price"]) if payload.get("average_fill_price") else None
            ),
            ts=datetime.now(UTC),
            detail=raw,
        )

    def trades_for_order(self, broker_order_id: str, segment: str = "FNO") -> list[Fill]:
        payload = self._get(
            f"/order/trades/{broker_order_id}", params={"segment": segment, "page": "0", "page_size": "50"}
        )
        rows = payload.get("trade_list") or payload.get("trades") or payload.get("data") or []
        fills: list[Fill] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                fills.append(
                    Fill(
                        fill_id=str(r.get("trade_id") or r.get("exchange_trade_id") or uuid.uuid4().hex),
                        order_reference_id=str(r.get("order_reference_id") or ""),
                        broker_order_id=broker_order_id,
                        trading_symbol=str(r.get("trading_symbol") or ""),
                        side=Side(str(r.get("transaction_type") or "BUY")),
                        quantity=int(r.get("quantity") or 0),
                        price=float(r.get("price") or 0.0),
                        ts=datetime.now(UTC),
                    )
                )
            except (TypeError, ValueError) as e:
                logger.warning("unparsable trade row for %s: %s", broker_order_id, e)
        return fills
