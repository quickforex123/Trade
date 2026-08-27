"""Research committee output contract.

A ResearchOpinion is context for the deterministic fusion layer. It is not a
signal, not an order, and carries no sizing. Schema-validation failure in the
committee discards the opinion entirely (fail closed) — there is no free-text
fallback anywhere in the decision path.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from qft.domain.enums import DataQuality, Direction, RecommendedAction, Regime
from qft.domain.time import ensure_utc


class ResearchOpinion(BaseModel):
    model_config = ConfigDict(frozen=True)

    opinion_id: str
    instrument: str  # underlying, e.g. "NIFTY"
    ts: datetime
    market_regime: Regime
    direction: Direction
    conviction: float = Field(ge=0.0, le=1.0)
    time_horizon: str  # e.g. "intraday", "1-3 days"
    supporting_evidence: tuple[str, ...] = ()
    contradicting_evidence: tuple[str, ...] = ()
    major_risks: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)
    news_risk: str = ""
    sentiment_context: str = ""
    technical_context: str = ""
    recommended_action: RecommendedAction
    confidence_quality: float = Field(ge=0.0, le=1.0)
    data_quality: DataQuality
    snapshot_id: str = ""
    committee_transcript_digest: str = ""

    _norm_ts = field_validator("ts")(ensure_utc)
