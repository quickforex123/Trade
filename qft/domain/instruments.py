"""Canonical instrument model.

Lot sizes, tick sizes, expiries and freeze quantities are DATA loaded from
the broker's instrument master — never constants (NIFTY lot size alone went
25 → 75 → 65 within twelve months).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, model_validator

from qft.domain.enums import Exchange, OptionType, Segment


class Instrument(BaseModel):
    """One tradeable contract (or index/equity underlier)."""

    model_config = ConfigDict(frozen=True)

    exchange: Exchange
    segment: Segment
    trading_symbol: str
    groww_symbol: str = ""
    exchange_token: str = ""
    underlying: str = ""
    instrument_type: str = ""  # e.g. IDX, FUT, CE, PE, EQ per master file
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    lot_size: int = 1
    tick_size: float = 0.05
    freeze_quantity: int | None = None

    @model_validator(mode="after")
    def _validate_option_fields(self) -> Instrument:
        if self.option_type is not None:
            if self.strike is None or self.strike <= 0:
                raise ValueError("option instrument requires a positive strike")
            if self.expiry is None:
                raise ValueError("option instrument requires an expiry")
        if self.lot_size < 1:
            raise ValueError("lot_size must be >= 1")
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")
        return self

    @property
    def key(self) -> str:
        """Stable identity used across the platform."""
        return f"{self.exchange}:{self.segment}:{self.trading_symbol}"

    @property
    def is_option(self) -> bool:
        return self.option_type is not None

    @property
    def is_future(self) -> bool:
        return self.segment is Segment.FNO and self.option_type is None and self.expiry is not None

    def round_to_tick(self, price: float) -> float:
        """Quantize a price to the contract's tick grid (round half away from zero)."""
        if price < 0:
            raise ValueError("price must be non-negative")
        ticks = round(price / self.tick_size)
        return round(ticks * self.tick_size, 2)
