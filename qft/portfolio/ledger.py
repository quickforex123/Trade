"""Event-sourced SQLite ledger.

Append-only `events` table is the system of record; positions, P&L and the
PortfolioView are DERIVED from it. Every event has an immutable id, a UTC
timestamp, a type, correlation ids and a JSON payload. Nothing is ever
updated or deleted.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from qft.domain.enums import Environment, Side
from qft.domain.ids import new_id
from qft.domain.orders import Fill
from qft.domain.portfolio import LedgerEventType, Position
from qft.domain.time import IST, ensure_utc, now_utc
from qft.risk.engine import PortfolioView

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    environment TEXT NOT NULL,
    type        TEXT NOT NULL,
    intent_id   TEXT,
    order_ref   TEXT,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts);
CREATE INDEX IF NOT EXISTS idx_events_order_ref ON events(order_ref);
"""


class Ledger:
    def __init__(self, path: Path | str, environment: Environment, initial_capital: float) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._env = environment
        self._initial_capital = initial_capital
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- append -----------------------------------------------------------------

    def append(
        self,
        event_type: LedgerEventType,
        payload: dict[str, Any],
        intent_id: str | None = None,
        order_ref: str | None = None,
        ts: datetime | None = None,
    ) -> str:
        event_id = new_id("ev")
        ts = ensure_utc(ts) if ts else now_utc()
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (event_id, ts, environment, type, intent_id, order_ref, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    ts.isoformat(),
                    self._env.value,
                    event_type.value,
                    intent_id,
                    order_ref,
                    json.dumps(payload, default=str, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        return event_id

    def record_fill(self, fill: Fill, intent_id: str | None = None) -> str:
        return self.append(
            LedgerEventType.FILL,
            fill.model_dump(mode="json"),
            intent_id=intent_id,
            order_ref=fill.order_reference_id,
            ts=fill.ts,
        )

    # -- queries -----------------------------------------------------------------

    def events(
        self,
        event_type: LedgerEventType | None = None,
        since: datetime | None = None,
        order_ref: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT event_id, ts, type, intent_id, order_ref, payload FROM events WHERE 1=1"
        args: list[str] = []
        if event_type is not None:
            query += " AND type = ?"
            args.append(event_type.value)
        if since is not None:
            query += " AND ts >= ?"
            args.append(ensure_utc(since).isoformat())
        if order_ref is not None:
            query += " AND order_ref = ?"
            args.append(order_ref)
        query += " ORDER BY ts ASC, event_id ASC"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [
            {
                "event_id": r[0],
                "ts": r[1],
                "type": r[2],
                "intent_id": r[3],
                "order_ref": r[4],
                "payload": json.loads(r[5]),
            }
            for r in rows
        ]

    def fills(self, since: datetime | None = None) -> list[Fill]:
        return [
            Fill.model_validate(e["payload"]) for e in self.events(LedgerEventType.FILL, since)
        ]

    # -- derived state --------------------------------------------------------------

    def positions(self) -> dict[str, Position]:
        """Net positions derived from all fills (FIFO-average)."""
        positions: dict[str, Position] = {}
        for fill in self.fills():
            positions[fill.trading_symbol] = _apply_fill(
                positions.get(fill.trading_symbol), fill
            )
        return dict(positions)

    def realized_pnl_between(self, start: datetime, end: datetime) -> float:
        """Realized P&L from fills in [start, end) minus fees recorded there."""
        start, end = ensure_utc(start), ensure_utc(end)
        # Replay ALL fills to carry correct cost basis, then take realized deltas in window.
        positions: dict[str, Position] = {}
        realized_in_window = 0.0
        for fill in self.fills():
            prev = positions.get(fill.trading_symbol)
            prev_realized = prev.realized_pnl if prev else 0.0
            updated = _apply_fill(prev, fill)
            positions[fill.trading_symbol] = updated
            if start <= fill.ts < end:
                realized_in_window += updated.realized_pnl - prev_realized
        fees = 0.0
        for e in self.events(LedgerEventType.FEES, since=start):
            ts = datetime.fromisoformat(e["ts"])
            if ts < end:
                fees += float(e["payload"].get("total", 0.0))
        return realized_in_window - fees

    def orders_submitted_between(self, start: datetime, end: datetime) -> int:
        start, end = ensure_utc(start), ensure_utc(end)
        n = 0
        for e in self.events(LedgerEventType.ORDER_REQUEST, since=start):
            if datetime.fromisoformat(e["ts"]) < end:
                n += 1
        return n

    def equity(self, unrealized: float = 0.0) -> float:
        genesis = datetime(2000, 1, 1, tzinfo=IST)
        return self._initial_capital + self.realized_pnl_between(genesis, now_utc()) + unrealized

    def high_water_mark(self) -> float:
        """Highest end-of-fill equity observed (realized basis)."""
        genesis = datetime(2000, 1, 1, tzinfo=IST)
        positions: dict[str, Position] = {}
        realized = 0.0
        hwm = self._initial_capital
        fees_events = [
            (datetime.fromisoformat(e["ts"]), float(e["payload"].get("total", 0.0)))
            for e in self.events(LedgerEventType.FEES, since=genesis)
        ]
        fee_idx = 0
        fees_so_far = 0.0
        for fill in self.fills():
            prev = positions.get(fill.trading_symbol)
            prev_realized = prev.realized_pnl if prev else 0.0
            updated = _apply_fill(prev, fill)
            positions[fill.trading_symbol] = updated
            realized += updated.realized_pnl - prev_realized
            while fee_idx < len(fees_events) and fees_events[fee_idx][0] <= fill.ts:
                fees_so_far += fees_events[fee_idx][1]
                fee_idx += 1
            hwm = max(hwm, self._initial_capital + realized - fees_so_far)
        return hwm

    def portfolio_view(
        self,
        now: datetime,
        unrealized_pnl: float = 0.0,
        margin_available: float | None = None,
        broker_connected: bool = True,
        reconciled: bool = True,
        open_strategy_ids: tuple[str, ...] = (),
        open_underlyings: tuple[str, ...] = (),
    ) -> PortfolioView:
        now = ensure_utc(now)
        ist_now_ = now.astimezone(IST)
        day_start = ist_now_.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())
        equity = self.equity(unrealized=unrealized_pnl)
        open_positions = [p for p in self.positions().values() if not p.is_flat]
        return PortfolioView(
            equity=max(equity, 1.0),
            cash_available=max(equity - sum(
                abs(p.net_quantity) * p.average_price for p in open_positions
            ), 0.0),
            margin_available=margin_available if margin_available is not None else max(equity, 0.0),
            high_water_mark=self.high_water_mark(),
            realized_pnl_today=self.realized_pnl_between(day_start, now),
            realized_pnl_week=self.realized_pnl_between(week_start, now),
            unrealized_pnl=unrealized_pnl,
            open_position_count=len(open_positions),
            open_underlyings=open_underlyings,
            open_strategy_ids=open_strategy_ids,
            orders_today=self.orders_submitted_between(day_start, now),
            broker_connected=broker_connected,
            reconciled=reconciled,
        )


def _apply_fill(prev: Position | None, fill: Fill) -> Position:
    signed = fill.quantity if fill.side is Side.BUY else -fill.quantity
    if prev is None or prev.net_quantity == 0:
        return Position(
            instrument_key=fill.trading_symbol,
            trading_symbol=fill.trading_symbol,
            net_quantity=signed,
            average_price=fill.price,
            realized_pnl=prev.realized_pnl if prev else 0.0,
            last_update=fill.ts,
        )
    new_qty = prev.net_quantity + signed
    realized = prev.realized_pnl
    if prev.net_quantity * signed < 0:
        closed = min(abs(signed), abs(prev.net_quantity))
        direction = 1 if prev.net_quantity > 0 else -1
        realized += direction * (fill.price - prev.average_price) * closed
        avg = prev.average_price if new_qty * prev.net_quantity > 0 else fill.price
    else:
        avg = (
            prev.average_price * abs(prev.net_quantity) + fill.price * abs(signed)
        ) / abs(new_qty)
    return Position(
        instrument_key=prev.instrument_key,
        trading_symbol=prev.trading_symbol,
        net_quantity=new_qty,
        average_price=avg if new_qty != 0 else 0.0,
        realized_pnl=realized,
        last_update=fill.ts,
    )
