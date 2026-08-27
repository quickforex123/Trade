"""Market-data provider protocol. Implementations: Groww read-only, simulated."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from qft.domain.instruments import Instrument
from qft.domain.market import Bar, OptionChain, Quote


class MarketDataProvider(Protocol):
    """Read-only market data. No implementation of this protocol may hold
    order-capable credentials."""

    def instruments(self) -> list[Instrument]:
        """Current instrument master (cached daily)."""
        ...

    def quote(self, instrument: Instrument) -> Quote: ...

    def option_chain(self, underlying: str, expiry: date) -> OptionChain: ...

    def expiries(self, underlying: str) -> list[date]: ...

    def historical_bars(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> list[Bar]: ...
