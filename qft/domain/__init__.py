"""Typed domain contracts shared by every layer.

Nothing in this package performs I/O and nothing here may import from any
other qft package. All models are Pydantic v2; models that represent facts
(snapshots, fills, decisions) are frozen.
"""

from qft.domain.enums import (
    DataQuality,
    Direction,
    Environment,
    Exchange,
    KillSwitch,
    OptionType,
    OrderState,
    OrderType,
    Product,
    RecommendedAction,
    Regime,
    Segment,
    Side,
    Validity,
)
from qft.domain.ids import deterministic_id, new_id
from qft.domain.instruments import Instrument
from qft.domain.market import (
    Bar,
    Depth,
    DepthLevel,
    FeedMeta,
    OptionChain,
    OptionQuote,
    Quote,
    VerifiedMarketSnapshot,
)
from qft.domain.orders import (
    ApprovedOrder,
    BrokerAck,
    Fill,
    OrderRequest,
    OrderStatus,
)
from qft.domain.portfolio import LedgerEventType, Position, TradeRecord
from qft.domain.research import ResearchOpinion
from qft.domain.risk import RiskDecision, RiskReason
from qft.domain.signals import Signal, TradeIntent
from qft.domain.time import IST, ist_now, now_utc, to_ist

__all__ = [
    "IST",
    "ApprovedOrder",
    "Bar",
    "BrokerAck",
    "DataQuality",
    "Depth",
    "DepthLevel",
    "Direction",
    "Environment",
    "Exchange",
    "FeedMeta",
    "Fill",
    "Instrument",
    "KillSwitch",
    "LedgerEventType",
    "OptionChain",
    "OptionQuote",
    "OptionType",
    "OrderRequest",
    "OrderState",
    "OrderStatus",
    "OrderType",
    "Position",
    "Product",
    "Quote",
    "RecommendedAction",
    "Regime",
    "ResearchOpinion",
    "RiskDecision",
    "RiskReason",
    "Segment",
    "Side",
    "Signal",
    "TradeIntent",
    "TradeRecord",
    "Validity",
    "VerifiedMarketSnapshot",
    "deterministic_id",
    "ist_now",
    "new_id",
    "now_utc",
    "to_ist",
]
