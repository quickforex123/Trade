"""Closed vocabularies. String-valued for readable ledgers and logs."""

from __future__ import annotations

from enum import StrEnum


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"


class Segment(StrEnum):
    CASH = "CASH"
    FNO = "FNO"


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_LOSS = "SL"
    STOP_LOSS_MARKET = "SL_M"


class Product(StrEnum):
    """Groww product types relevant to us. MIS = intraday, NRML = carry."""

    MIS = "MIS"
    NRML = "NRML"
    CNC = "CNC"


class Validity(StrEnum):
    DAY = "DAY"
    IOC = "IOC"


class OrderState(StrEnum):
    """Execution-side order lifecycle. `place_order` returning is NOT a fill."""

    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"  # persisted before the network call
    ACKED = "ACKED"  # broker accepted, has broker order id
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"  # timeout after submit; must reconcile by reference id


class Environment(StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"


class Regime(StrEnum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    MEAN_REVERTING = "MEAN_REVERTING"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    BREAKOUT = "BREAKOUT"
    EVENT_RISK = "EVENT_RISK"
    EXPIRY_REGIME = "EXPIRY_REGIME"
    ILLIQUID = "ILLIQUID"
    NO_TRADE = "NO_TRADE"


class KillSwitch(StrEnum):
    NONE = "NONE"
    SOFT = "SOFT"  # no new entries; exits allowed
    HARD = "HARD"  # no orders except operator-confirmed flatten


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class RecommendedAction(StrEnum):
    """Research committee output vocabulary. Deliberately NOT buy/sell."""

    FAVOR = "FAVOR"
    NEUTRAL = "NEUTRAL"
    AVOID = "AVOID"


class DataQuality(StrEnum):
    GOOD = "GOOD"
    DEGRADED = "DEGRADED"
    BAD = "BAD"
