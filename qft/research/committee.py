"""Research committee: analysts → adversarial debate → risk personas → opinion.

Adapted from the audited TradingAgents architecture with these deliberate
changes (see ARCHITECTURE_AUDIT.md):
- Output is a ResearchOpinion (context), never BUY/SELL.
- Every stage is schema-enforced; any stage failure aborts the run and
  returns None (fail closed) — no free-text fallback.
- All market numbers come from the injected VerifiedMarketSnapshot; prompts
  forbid inventing figures, and the snapshot is the only numeric source shown.
- Disagreement is preserved: the bear's best case and the conservative
  analyst's objections travel verbatim into the final opinion.
- Deterministic sequential orchestration (auditable transcript), not a graph.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from qft.domain.enums import DataQuality, Direction, RecommendedAction, Regime
from qft.domain.ids import new_id
from qft.domain.market import VerifiedMarketSnapshot
from qft.domain.research import ResearchOpinion
from qft.domain.time import ensure_utc
from qft.features.engine import FeatureFrame
from qft.research.llm import StructuredLLM

logger = logging.getLogger(__name__)


# -- stage schemas ------------------------------------------------------------


class TechnicalView(BaseModel):
    model_config = ConfigDict(frozen=True)
    trend_assessment: str
    momentum_assessment: str
    volatility_assessment: str
    key_levels: str
    bias: Direction
    confidence: float = Field(ge=0, le=1)


class ContextView(BaseModel):
    """News/sentiment context. Sources may be empty — say so, never invent."""

    model_config = ConfigDict(frozen=True)
    news_risk: str
    sentiment_summary: str
    event_flags: tuple[str, ...] = ()
    data_coverage: str  # honest statement of what was actually available


class DebateCase(BaseModel):
    model_config = ConfigDict(frozen=True)
    thesis: str
    strongest_points: tuple[str, ...] = Field(min_length=1, max_length=5)
    attack_on_opponent: str
    what_would_prove_me_wrong: tuple[str, ...] = Field(min_length=1, max_length=4)


class RiskPersonaView(BaseModel):
    model_config = ConfigDict(frozen=True)
    stance: str
    hidden_risks: tuple[str, ...] = ()
    liquidity_concerns: str = ""
    overconfidence_flags: tuple[str, ...] = ()
    reward_risk_view: str = ""


class CommitteeDecision(BaseModel):
    model_config = ConfigDict(frozen=True)
    direction: Direction
    conviction: float = Field(ge=0, le=1)
    recommended_action: RecommendedAction
    time_horizon: str
    synthesis: str
    invalidation_conditions: tuple[str, ...] = Field(min_length=1, max_length=6)
    unresolved_disagreements: tuple[str, ...] = ()
    confidence_quality: float = Field(ge=0, le=1)


_FORBID = (
    "Use ONLY the numbers provided in the VERIFIED MARKET SNAPSHOT section. "
    "Never invent prices, indicator values, news, or data that was not provided. "
    "If information is missing, say it is missing."
)


class ResearchCommittee:
    def __init__(self, deep: StructuredLLM, quick: StructuredLLM) -> None:
        self._deep = deep
        self._quick = quick

    def run(
        self,
        underlying: str,
        snapshot: VerifiedMarketSnapshot,
        frame: FeatureFrame,
        regime: Regime,
        as_of: datetime,
        news_digest: str = "",
        sentiment_digest: str = "",
        memory_context: str = "",
    ) -> ResearchOpinion | None:
        """Run the committee. Returns None when any stage fails (fail closed)."""
        as_of = ensure_utc(as_of)
        facts = self._render_facts(underlying, snapshot, frame, regime)
        transcript: list[str] = [facts]

        tech = self._quick_stage(
            TechnicalView,
            "You are the Technical Analyst on an intraday NIFTY derivatives desk.",
            f"{facts}\n\nAssess trend, momentum, volatility and key levels. {_FORBID}",
        )
        if tech is None:
            return None
        transcript.append(f"TECHNICAL: {tech.model_dump_json()}")

        context = self._quick_stage(
            ContextView,
            "You are the News & Sentiment Analyst. You report honestly on coverage gaps.",
            (
                f"{facts}\n\nNEWS INPUT (may be empty):\n{news_digest or '(none provided)'}\n\n"
                f"SENTIMENT INPUT (may be empty):\n{sentiment_digest or '(none provided)'}\n\n"
                f"Summarize news risk and sentiment. State clearly what data was NOT available. {_FORBID}"
            ),
        )
        if context is None:
            return None
        transcript.append(f"CONTEXT: {context.model_dump_json()}")

        bull = self._quick_stage(
            DebateCase,
            "You are the Bull Researcher. Argue the strongest LONG case, then state what would prove you wrong.",
            f"{facts}\n\nTECHNICAL VIEW: {tech.model_dump_json()}\nCONTEXT: {context.model_dump_json()}\n{_FORBID}",
        )
        if bull is None:
            return None
        bear = self._quick_stage(
            DebateCase,
            "You are the Bear Researcher. Attack the bull case ruthlessly and argue the strongest SHORT/stand-aside case; state what would prove you wrong.",
            (
                f"{facts}\n\nTECHNICAL VIEW: {tech.model_dump_json()}\nCONTEXT: {context.model_dump_json()}\n"
                f"BULL CASE TO ATTACK: {bull.model_dump_json()}\n{_FORBID}"
            ),
        )
        if bear is None:
            return None
        transcript += [f"BULL: {bull.model_dump_json()}", f"BEAR: {bear.model_dump_json()}"]

        personas: dict[str, RiskPersonaView] = {}
        persona_prompts = {
            "aggressive": "You are the Aggressive Risk Analyst: where is the case for taking MORE risk here?",
            "neutral": "You are the Neutral Risk Analyst: weigh both sides dispassionately.",
            "conservative": (
                "You are the Conservative Risk Analyst. Hunt specifically for: hidden downside, "
                "liquidity problems, event risk, overconfidence in the debate, weak or stale evidence, "
                "poor reward/risk, and large tail exposure. Be adversarial."
            ),
        }
        for name, sys_prompt in persona_prompts.items():
            view = self._quick_stage(
                RiskPersonaView,
                sys_prompt,
                (
                    f"{facts}\nBULL: {bull.model_dump_json()}\nBEAR: {bear.model_dump_json()}\n"
                    f"CONTEXT: {context.model_dump_json()}\n{_FORBID}"
                ),
            )
            if view is None:
                return None
            personas[name] = view
            transcript.append(f"RISK[{name}]: {view.model_dump_json()}")

        decision = self._deep_stage(
            CommitteeDecision,
            (
                "You are the Research Manager synthesizing an intraday committee view for NIFTY "
                "derivatives. Your output is CONTEXT for a deterministic trading system — not an "
                "order. Preserve genuine disagreement rather than papering over it. Direction "
                "NEUTRAL with action AVOID is a fully acceptable, often correct, outcome."
            ),
            (
                f"{facts}\n\nTECHNICAL: {tech.model_dump_json()}\nCONTEXT: {context.model_dump_json()}\n"
                f"BULL: {bull.model_dump_json()}\nBEAR: {bear.model_dump_json()}\n"
                f"AGGRESSIVE: {personas['aggressive'].model_dump_json()}\n"
                f"NEUTRAL: {personas['neutral'].model_dump_json()}\n"
                f"CONSERVATIVE: {personas['conservative'].model_dump_json()}\n"
                f"PRIOR LESSONS (may be empty):\n{memory_context or '(none)'}\n\n"
                f"Synthesize. Every directional view MUST list concrete invalidation conditions. {_FORBID}"
            ),
        )
        if decision is None:
            return None
        transcript.append(f"DECISION: {decision.model_dump_json()}")

        data_quality = (
            DataQuality.GOOD
            if snapshot.data_quality is DataQuality.GOOD and news_digest and sentiment_digest
            else DataQuality.DEGRADED
        )
        digest = hashlib.sha256("\n".join(transcript).encode()).hexdigest()[:16]

        return ResearchOpinion(
            opinion_id=new_id("op"),
            instrument=underlying,
            ts=as_of,
            market_regime=regime,
            direction=decision.direction,
            conviction=decision.conviction,
            time_horizon=decision.time_horizon,
            supporting_evidence=bull.strongest_points,
            contradicting_evidence=bear.strongest_points,
            major_risks=tuple(personas["conservative"].hidden_risks)[:6],
            invalidation_conditions=decision.invalidation_conditions,
            news_risk=context.news_risk,
            sentiment_context=context.sentiment_summary,
            technical_context=tech.trend_assessment,
            recommended_action=decision.recommended_action,
            confidence_quality=decision.confidence_quality,
            data_quality=data_quality,
            snapshot_id=snapshot.snapshot_id,
            committee_transcript_digest=digest,
        )

    # -- internals ----------------------------------------------------------------

    def _quick_stage(self, schema, system: str, prompt: str):
        result = self._quick.generate(system, prompt, schema)
        if result is None:
            logger.error("committee stage %s failed — aborting run (fail closed)", schema.__name__)
        return result

    def _deep_stage(self, schema, system: str, prompt: str):
        result = self._deep.generate(system, prompt, schema)
        if result is None:
            logger.error("committee stage %s failed — aborting run (fail closed)", schema.__name__)
        return result

    @staticmethod
    def _render_facts(
        underlying: str,
        snapshot: VerifiedMarketSnapshot,
        frame: FeatureFrame,
        regime: Regime,
    ) -> str:
        lines = [
            "VERIFIED MARKET SNAPSHOT (sole numeric source of truth):",
            f"underlying={underlying} as_of={snapshot.as_of.isoformat()} "
            f"verified={snapshot.verified} quality={snapshot.data_quality}",
            f"regime={regime}",
        ]
        if snapshot.spot is not None:
            lines.append(f"spot ltp={snapshot.spot.ltp}")
        if snapshot.future is not None:
            lines.append(
                f"future ltp={snapshot.future.ltp} bid={snapshot.future.bid} ask={snapshot.future.ask}"
            )
        if snapshot.chain is not None:
            lines.append(
                f"chain expiry={snapshot.chain.expiry} underlying_price={snapshot.chain.underlying_price} "
                f"rows={len(snapshot.chain.rows)}"
            )
        feats = ", ".join(f"{k}={v:.6g}" for k, v in sorted(frame.features.items()))
        lines.append(f"features[{frame.as_of.isoformat()}]: {feats}")
        return "\n".join(lines)
