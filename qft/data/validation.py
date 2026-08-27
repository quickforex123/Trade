"""Snapshot validation — the only factory for verified market snapshots.

Fail-closed: anything questionable produces verified=False and the risk
engine will refuse the trade. The validator never repairs data.
"""

from __future__ import annotations

from datetime import datetime

from qft.data.calendar import NSECalendar
from qft.domain.enums import DataQuality
from qft.domain.ids import new_id
from qft.domain.market import OptionChain, Quote, VerifiedMarketSnapshot
from qft.domain.time import ensure_utc


class SnapshotValidator:
    def __init__(
        self,
        calendar: NSECalendar,
        max_tick_age_seconds: float = 3.0,
        max_snapshot_age_seconds: float = 90.0,
        max_move_pct_between_ticks: float = 0.05,
    ) -> None:
        self._calendar = calendar
        self._max_tick_age = max_tick_age_seconds
        self._max_snapshot_age = max_snapshot_age_seconds
        self._max_move = max_move_pct_between_ticks
        self._last_ltp: dict[str, float] = {}

    def build(
        self,
        as_of: datetime,
        underlying: str,
        spot: Quote | None = None,
        future: Quote | None = None,
        chain: OptionChain | None = None,
        quotes: tuple[Quote, ...] = (),
    ) -> VerifiedMarketSnapshot:
        as_of = ensure_utc(as_of)
        issues: list[str] = []
        session_open = self._calendar.is_market_open(as_of)

        all_quotes: list[Quote] = [q for q in (spot, future) if q is not None]
        all_quotes.extend(quotes)
        if chain is not None:
            all_quotes.extend(row.quote for row in chain.rows)

        if not all_quotes:
            issues.append("no quotes supplied")

        for q in all_quotes:
            age = q.meta.age_seconds(as_of)
            if age < -1.0:
                issues.append(f"{q.instrument_key}: timestamp {abs(age):.1f}s in the future")
            elif age > self._max_tick_age:
                issues.append(f"{q.instrument_key}: stale by {age:.1f}s (max {self._max_tick_age})")
            prev = self._last_ltp.get(q.instrument_key)
            if prev is not None and prev > 0 and q.ltp > 0:
                move = abs(q.ltp - prev) / prev
                if move > self._max_move:
                    issues.append(
                        f"{q.instrument_key}: implausible move {move:.1%} vs previous tick"
                    )

        if chain is not None:
            if chain.underlying_price <= 0:
                issues.append("chain underlying price non-positive")
            chain_age = chain.meta.age_seconds(as_of)
            if chain_age > self._max_snapshot_age:
                issues.append(f"option chain stale by {chain_age:.1f}s")

        if not session_open:
            issues.append("market session closed")

        verified = not issues
        if verified:
            for q in all_quotes:
                if q.ltp > 0:
                    self._last_ltp[q.instrument_key] = q.ltp

        quality = (
            DataQuality.GOOD
            if verified
            else (DataQuality.DEGRADED if session_open and all_quotes else DataQuality.BAD)
        )
        return VerifiedMarketSnapshot(
            snapshot_id=new_id("snap"),
            as_of=as_of,
            underlying=underlying,
            spot=spot,
            future=future,
            chain=chain,
            quotes=quotes,
            session_open=session_open,
            verified=verified,
            data_quality=quality,
            issues=tuple(issues),
        )
