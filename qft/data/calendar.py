"""NSE session calendar (IST-anchored).

Holiday lists are DATA maintained in config; expiry days come from the broker
API, never computed from weekday rules (the weekly expiry day has changed by
exchange circular before and will again).
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from qft.domain.time import IST

_MARKET_OPEN = time(9, 15)
_MARKET_CLOSE = time(15, 30)

# 2026 NSE trading holidays (equity & F&O). Maintained by hand from the NSE
# circular; verify against the exchange list at the start of each year.
_DEFAULT_HOLIDAYS_2026: frozenset[str] = frozenset(
    {
        "2026-01-26",  # Republic Day
        "2026-03-03",  # Holi
        "2026-03-21",  # Id-Ul-Fitr
        "2026-04-01",  # Annual bank closing
        "2026-04-03",  # Good Friday
        "2026-04-14",  # Dr. Ambedkar Jayanti
        "2026-05-01",  # Maharashtra Day
        "2026-05-28",  # Bakri Id
        "2026-08-15",  # Independence Day (Saturday in 2026; kept for safety)
        "2026-09-14",  # Ganesh Chaturthi
        "2026-10-02",  # Gandhi Jayanti
        "2026-10-20",  # Diwali (Laxmi Pujan) — Muhurat session handled separately
        "2026-11-09",  # Guru Nanak Jayanti (indicative)
        "2026-12-25",  # Christmas
    }
)


class NSECalendar:
    def __init__(self, holidays: frozenset[str] | None = None) -> None:
        self._holidays = holidays if holidays is not None else _DEFAULT_HOLIDAYS_2026

    def is_trading_day(self, d: date) -> bool:
        if d.weekday() >= 5:
            return False
        return d.isoformat() not in self._holidays

    def is_market_open(self, ts: datetime) -> bool:
        """Regular session only; ts must be tz-aware."""
        if ts.tzinfo is None:
            raise ValueError("naive datetime rejected")
        ist = ts.astimezone(IST)
        if not self.is_trading_day(ist.date()):
            return False
        return _MARKET_OPEN <= ist.time() < _MARKET_CLOSE

    def session_bounds_utc(self, d: date) -> tuple[datetime, datetime] | None:
        if not self.is_trading_day(d):
            return None
        tz: ZoneInfo = IST
        open_ist = datetime.combine(d, _MARKET_OPEN, tzinfo=tz)
        close_ist = datetime.combine(d, _MARKET_CLOSE, tzinfo=tz)
        return open_ist.astimezone(ZoneInfo("UTC")), close_ist.astimezone(ZoneInfo("UTC"))

    def next_trading_day(self, d: date) -> date:
        cur = d
        for _ in range(30):
            cur = date.fromordinal(cur.toordinal() + 1)
            if self.is_trading_day(cur):
                return cur
        raise RuntimeError("no trading day found within 30 days — holiday data broken")
