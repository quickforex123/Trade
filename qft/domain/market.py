"""Market-data contracts. Every message carries provenance and freshness."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qft.domain.enums import DataQuality
from qft.domain.instruments import Instrument
from qft.domain.time import ensure_utc


class FeedMeta(BaseModel):
    """Provenance stamp attached to every market-data message."""

    model_config = ConfigDict(frozen=True)

    source: str  # e.g. "groww_rest", "groww_ws", "sim"
    receive_ts: datetime
    exchange_ts: datetime | None = None

    _norm_recv = field_validator("receive_ts")(ensure_utc)

    @field_validator("exchange_ts")
    @classmethod
    def _norm_exch(cls, v: datetime | None) -> datetime | None:
        return None if v is None else ensure_utc(v)

    def age_seconds(self, as_of: datetime) -> float:
        """Freshness relative to `as_of` (prefer exchange timestamp when present)."""
        basis = self.exchange_ts or self.receive_ts
        return (ensure_utc(as_of) - basis).total_seconds()


class Bar(BaseModel):
    """One OHLCV candle. `ts` is the bar OPEN time, UTC."""

    model_config = ConfigDict(frozen=True)

    instrument_key: str
    ts: datetime
    interval: str  # "1m", "5m", "15m", "1d"
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    open_interest: float | None = None

    _norm_ts = field_validator("ts")(ensure_utc)

    @model_validator(mode="after")
    def _sane(self) -> Bar:
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(f"incoherent OHLC for {self.instrument_key} @ {self.ts}")
        if self.low < 0 or self.volume < 0:
            raise ValueError("negative price/volume")
        return self


class DepthLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: float
    quantity: int


class Depth(BaseModel):
    model_config = ConfigDict(frozen=True)

    bids: tuple[DepthLevel, ...] = ()
    asks: tuple[DepthLevel, ...] = ()


class Quote(BaseModel):
    """Top-of-book quote for one instrument."""

    model_config = ConfigDict(frozen=True)

    instrument_key: str
    meta: FeedMeta
    ltp: float
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: float | None = None
    open_interest: float | None = None
    depth: Depth | None = None

    @model_validator(mode="after")
    def _sane(self) -> Quote:
        if self.ltp < 0:
            raise ValueError("negative LTP")
        if self.bid is not None and self.ask is not None and self.bid > self.ask > 0:
            raise ValueError(f"crossed book bid={self.bid} ask={self.ask}")
        return self

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return None

    @property
    def spread_pct(self) -> float | None:
        """Spread as a fraction of mid; None when book is one-sided/absent."""
        m = self.mid
        if m is None or m <= 0:
            return None
        assert self.bid is not None and self.ask is not None
        return (self.ask - self.bid) / m


class OptionQuote(BaseModel):
    """One option-chain row: quote plus greeks/OI when provided."""

    model_config = ConfigDict(frozen=True)

    instrument: Instrument
    quote: Quote
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    oi: float | None = None
    oi_change: float | None = None


class OptionChain(BaseModel):
    model_config = ConfigDict(frozen=True)

    underlying: str
    expiry: str  # ISO date
    meta: FeedMeta
    underlying_price: float
    rows: tuple[OptionQuote, ...] = ()


class VerifiedMarketSnapshot(BaseModel):
    """The only object trading decisions may take prices from.

    Built exclusively by qft.data.validation.SnapshotValidator, which checks
    schema, freshness, internal sanity and session consistency. `verified` is
    set by the validator; anything else constructing a verified snapshot is a
    bug. Fail closed: no verified snapshot => no trade.
    """

    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    as_of: datetime
    underlying: str
    spot: Quote | None = None
    future: Quote | None = None
    chain: OptionChain | None = None
    quotes: tuple[Quote, ...] = ()
    session_open: bool = False
    verified: bool = False
    data_quality: DataQuality = DataQuality.BAD
    issues: tuple[str, ...] = Field(default=())

    _norm_asof = field_validator("as_of")(ensure_utc)

    def quote_for(self, instrument_key: str) -> Quote | None:
        if self.spot is not None and self.spot.instrument_key == instrument_key:
            return self.spot
        if self.future is not None and self.future.instrument_key == instrument_key:
            return self.future
        for q in self.quotes:
            if q.instrument_key == instrument_key:
                return q
        if self.chain is not None:
            for row in self.chain.rows:
                if row.instrument.key == instrument_key:
                    return row.quote
        return None
