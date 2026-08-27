"""GrowwExecutionAdapter — the ONLY component allowed to hold the production
Groww credentials, and the only one that can place real orders.

Security invariants:
- Credentials come exclusively from the process environment at construction
  (GROWW_API_KEY + GROWW_API_SECRET or GROWW_API_TOTP), injected by the
  operator's secret manager into the isolated daemon process. Nothing here
  reads config files, and no other qft module imports this one except
  qft.execution composition for LIVE.
- Construction REFUSES to proceed unless QFT_ENVIRONMENT=LIVE, so importing
  or instantiating it accidentally in research/paper/shadow processes fails.
- order_reference_id is ALWAYS ours (deterministic from the intent); we never
  use the SDK's random default.

Endpoint contract: docs/GROWW_API_REFERENCE.md (growwapi==1.5.0). The hosted
official docs win over both if they differ.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from qft.brokers.base import BrokerError, BrokerTimeout
from qft.brokers.groww_readonly_account import _STATUS_MAP, GrowwReadOnlyAdapter
from qft.domain.orders import ApprovedOrder, BrokerAck, Fill, OrderStatus
from qft.domain.portfolio import Position

logger = logging.getLogger(__name__)

_BASE = "https://api.groww.in/v1"


def _get_access_token(client: httpx.Client) -> str:
    """Exchange API key + (approval secret | TOTP) for an access token.

    Flow verified from the official SDK: POST /token/api/access with either
    {key_type: totp, totp} or {key_type: approval, checksum, timestamp}.
    """
    api_key = os.environ.get("GROWW_API_KEY", "")
    secret = os.environ.get("GROWW_API_SECRET", "")
    totp = os.environ.get("GROWW_API_TOTP", "")
    if not api_key or not (secret or totp):
        raise BrokerError(
            "GROWW_API_KEY plus GROWW_API_SECRET or GROWW_API_TOTP must be present in the "
            "daemon environment (and ONLY there)"
        )
    if secret:
        timestamp = int(time.time())
        checksum = hashlib.sha256(f"{secret}{timestamp}".encode()).hexdigest()
        data: dict[str, Any] = {"key_type": "approval", "checksum": checksum, "timestamp": timestamp}
    else:
        data = {"key_type": "totp", "totp": totp.strip()}
    resp = client.post(
        f"{_BASE}/token/api/access",
        json=data,
        headers={
            "x-request-id": str(uuid.uuid4()),
            "Authorization": f"Bearer {api_key}",
            "x-api-version": "1.0",
        },
        timeout=15.0,
    )
    if resp.status_code >= 400:
        raise BrokerError(f"token exchange failed: HTTP {resp.status_code}")
    token = resp.json().get("token")
    if not token:
        raise BrokerError("token exchange returned no token")
    return str(token)


class GrowwExecutionAdapter:
    """Implements qft.brokers.base.BrokerAdapter against the real venue."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        if os.environ.get("QFT_ENVIRONMENT") != "LIVE":
            raise BrokerError(
                "GrowwExecutionAdapter refuses to construct outside QFT_ENVIRONMENT=LIVE"
            )
        self._client = client or httpx.Client(timeout=10.0)
        self._token = _get_access_token(self._client)
        self._reader = GrowwReadOnlyAdapter(self._token, client=self._client)

    # -- plumbing ---------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "x-request-id": str(uuid.uuid4()),
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "x-api-version": "1.0",
        }

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(_BASE + path, json=body, headers=self._headers())
        except httpx.TimeoutException as e:
            # Ambiguous: caller MUST reconcile via order_status_by_reference.
            raise BrokerTimeout(f"timeout on {path}") from e
        if resp.status_code == 429:
            raise BrokerError("rate limited by broker")
        body_json = resp.json()
        if isinstance(body_json, dict) and body_json.get("status") == "FAILURE":
            err = body_json.get("error") or {}
            raise BrokerError(f"broker rejected: {err.get('code')} {err.get('message')}")
        if resp.status_code >= 400:
            raise BrokerError(f"HTTP {resp.status_code} on {path}")
        payload = body_json.get("payload", body_json) if isinstance(body_json, dict) else body_json
        return payload if isinstance(payload, dict) else {"data": payload}

    # -- BrokerAdapter ------------------------------------------------------------

    def place(self, order: ApprovedOrder) -> BrokerAck:
        payload = self._post(
            "/order/create",
            {
                "trading_symbol": order.trading_symbol,
                "quantity": order.quantity,
                "price": order.price or 0.0,
                "trigger_price": order.trigger_price,
                "validity": order.validity.value,
                "exchange": order.exchange.value,
                "segment": order.segment.value,
                "product": order.product.value,
                "order_type": order.order_type.value,
                "transaction_type": order.side.value,
                "order_reference_id": order.order_reference_id,  # OUR deterministic id
            },
        )
        broker_id = str(payload.get("groww_order_id") or "")
        if not broker_id:
            raise BrokerError("order create returned no groww_order_id")
        return BrokerAck(
            order_reference_id=order.order_reference_id,
            broker_order_id=broker_id,
            ts=datetime.now(UTC),
            raw_status=str(payload.get("order_status") or ""),
        )

    def cancel(self, order_reference_id: str) -> OrderStatus:
        status = self._reader.order_status_by_reference(order_reference_id)
        if status is None or not status.broker_order_id:
            raise BrokerError(f"cannot cancel unknown order {order_reference_id}")
        self._post("/order/cancel", {"segment": "FNO", "groww_order_id": status.broker_order_id})
        refreshed = self._reader.order_status_by_reference(order_reference_id)
        return refreshed if refreshed is not None else status

    def order_status(self, broker_order_id: str) -> OrderStatus:
        resp = self._client.get(
            f"{_BASE}/order/status/{broker_order_id}",
            params={"segment": "FNO"},
            headers=self._headers(),
        )
        if resp.status_code >= 400:
            raise BrokerError(f"status HTTP {resp.status_code}")
        payload = resp.json().get("payload", {})
        raw = str(payload.get("order_status") or "").upper()
        from qft.domain.enums import OrderState

        return OrderStatus(
            order_reference_id=str(payload.get("order_reference_id") or ""),
            broker_order_id=broker_order_id,
            state=_STATUS_MAP.get(raw, OrderState.UNKNOWN),
            filled_quantity=int(payload.get("filled_quantity") or 0),
            pending_quantity=int(payload.get("pending_quantity") or 0),
            average_fill_price=(
                float(payload["average_fill_price"]) if payload.get("average_fill_price") else None
            ),
            ts=datetime.now(UTC),
            detail=raw,
        )

    def order_status_by_reference(self, order_reference_id: str) -> OrderStatus | None:
        return self._reader.order_status_by_reference(order_reference_id)

    def trades_for_order(self, broker_order_id: str) -> list[Fill]:
        return self._reader.trades_for_order(broker_order_id)

    def positions(self) -> list[Position]:
        return self._reader.positions()

    def available_margin(self) -> float:
        return self._reader.available_margin()
