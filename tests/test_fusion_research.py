"""Fusion engine rules and research committee (fail-closed) tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from qft.config.risk_limits import RiskLimits
from qft.costs.model import CostModel
from qft.data.validation import SnapshotValidator
from qft.domain.enums import (
    DataQuality,
    Direction,
    Exchange,
    OptionType,
    RecommendedAction,
    Regime,
    Segment,
    Side,
)
from qft.domain.ids import new_id
from qft.domain.instruments import Instrument
from qft.domain.market import FeedMeta, OptionChain, OptionQuote, Quote
from qft.domain.research import ResearchOpinion
from qft.domain.signals import Signal
from qft.features.engine import FeatureEngine
from qft.fusion.engine import FusionConfig, SignalFusionEngine
from qft.research.committee import (
    CommitteeDecision,
    ContextView,
    DebateCase,
    ResearchCommittee,
    RiskPersonaView,
    TechnicalView,
)
from qft.research.llm import FakeLLM
from qft.research.memory import MemoryStore
from tests.conftest import TRADING_TS, make_quote
from tests.synthetic import synth_day

pytestmark = pytest.mark.unit

LIMITS = RiskLimits(initial_capital=50_000)


def _chain_row(strike: float, ot: OptionType, ltp: float, oi: float = 2e6,
               spread_bp: float = 40) -> OptionQuote:
    sym = f"NIFTY26SEP{int(strike)}{ot.value}"
    inst = Instrument(
        exchange=Exchange.NSE, segment=Segment.FNO, trading_symbol=sym,
        underlying="NIFTY", instrument_type=ot.value, expiry="2026-09-01",
        strike=strike, option_type=ot, lot_size=65,
    )
    half = ltp * spread_bp / 10_000 / 2
    q = Quote(
        instrument_key=inst.key,
        meta=FeedMeta(source="test", receive_ts=TRADING_TS - timedelta(seconds=1),
                      exchange_ts=TRADING_TS - timedelta(seconds=1)),
        ltp=ltp, bid=round(ltp - half, 2), ask=round(ltp + half, 2),
        open_interest=oi, volume=1e6,
    )
    return OptionQuote(instrument=inst, quote=q, oi=oi, delta=None)


def make_verified_snapshot(calendar, spot=24_500.0):
    rows = []
    for strike in (24_300, 24_400, 24_500, 24_600, 24_700):
        # cheap weekly-style premiums so 1 lot fits a Rs.50k risk budget
        premium_ce = max(30.0, spot - strike + 32)
        premium_pe = max(30.0, strike - spot + 32)
        rows.append(_chain_row(strike, OptionType.CE, premium_ce))
        rows.append(_chain_row(strike, OptionType.PE, premium_pe))
    chain = OptionChain(
        underlying="NIFTY", expiry="2026-09-01",
        meta=FeedMeta(source="test", receive_ts=TRADING_TS - timedelta(seconds=1)),
        underlying_price=spot, rows=tuple(rows),
    )
    v = SnapshotValidator(calendar)
    return v.build(TRADING_TS, "NIFTY",
                   spot=make_quote("NSE:CASH:NIFTY", spot), chain=chain)


def make_signal(direction=Direction.LONG, strength=0.7) -> Signal:
    return Signal(
        signal_id=new_id("sig"), strategy_id="orb_v1", strategy_version="1.0.0",
        ts=TRADING_TS, underlying="NIFTY", direction=direction, strength=strength,
        regime=Regime.BREAKOUT, rationale="TEST",
    )


def make_opinion(direction=Direction.LONG, conviction=0.8,
                 action=RecommendedAction.FAVOR, quality=DataQuality.GOOD) -> ResearchOpinion:
    return ResearchOpinion(
        opinion_id=new_id("op"), instrument="NIFTY", ts=TRADING_TS,
        market_regime=Regime.BREAKOUT, direction=direction, conviction=conviction,
        time_horizon="intraday", invalidation_conditions=("break below VWAP",),
        recommended_action=action, confidence_quality=0.7, data_quality=quality,
    )


def fusion() -> SignalFusionEngine:
    return SignalFusionEngine(LIMITS, CostModel(), FusionConfig())


def test_fusion_produces_defined_risk_long_premium(calendar) -> None:
    snap = make_verified_snapshot(calendar)
    assert snap.verified, snap.issues
    intent = fusion().fuse(make_signal(), snap, Regime.BREAKOUT)
    assert intent is not None
    assert intent.side is Side.BUY
    assert intent.option_type is OptionType.CE  # LONG -> CE
    assert intent.lots == 1
    assert intent.quantity == 65
    assert intent.estimated_max_loss > 0
    assert intent.snapshot_id == snap.snapshot_id
    assert intent.stop_loss_points > 0


def test_fusion_short_signal_buys_puts(calendar) -> None:
    snap = make_verified_snapshot(calendar)
    intent = fusion().fuse(make_signal(Direction.SHORT), snap, Regime.TRENDING_DOWN)
    assert intent is not None
    assert intent.option_type is OptionType.PE
    assert intent.side is Side.BUY  # never a naked short


def test_fusion_no_trade_regimes(calendar) -> None:
    snap = make_verified_snapshot(calendar)
    for regime in (Regime.NO_TRADE, Regime.EVENT_RISK, Regime.ILLIQUID):
        assert fusion().fuse(make_signal(), snap, regime) is None


def test_fusion_unverified_snapshot_refused(calendar) -> None:
    v = SnapshotValidator(calendar)
    snap = v.build(TRADING_TS, "NIFTY")  # empty => unverified
    assert fusion().fuse(make_signal(), snap, Regime.BREAKOUT) is None


def test_fusion_weak_signal_refused(calendar) -> None:
    snap = make_verified_snapshot(calendar)
    assert fusion().fuse(make_signal(strength=0.1), snap, Regime.BREAKOUT) is None


def test_opinion_boost_and_veto(calendar) -> None:
    snap = make_verified_snapshot(calendar)
    f = fusion()
    base = f.fuse(make_signal(strength=0.5), snap, Regime.BREAKOUT)
    assert base is not None

    boosted = f.fuse(make_signal(strength=0.5), snap, Regime.BREAKOUT,
                     opinion=make_opinion())
    assert boosted is not None
    assert boosted.quant_confidence > base.quant_confidence
    assert "boosted" in boosted.ai_research_context

    vetoed = f.fuse(make_signal(strength=0.9), snap, Regime.BREAKOUT,
                    opinion=make_opinion(direction=Direction.SHORT, conviction=0.9))
    assert vetoed is None

    avoided = f.fuse(make_signal(strength=0.9), snap, Regime.BREAKOUT,
                     opinion=make_opinion(action=RecommendedAction.AVOID, conviction=0.9))
    assert avoided is None


def test_opinion_dampen_below_floor_blocks(calendar) -> None:
    snap = make_verified_snapshot(calendar)
    f = fusion()
    damped = f.fuse(
        make_signal(strength=0.45), snap, Regime.BREAKOUT,
        opinion=make_opinion(direction=Direction.SHORT, conviction=0.5,
                             action=RecommendedAction.NEUTRAL),
    )
    # 0.45 * 0.7 = 0.315 < 0.35 floor
    assert damped is None


def test_stale_opinion_ignored(calendar) -> None:
    snap = make_verified_snapshot(calendar)
    old = make_opinion().model_copy(update={"ts": TRADING_TS - timedelta(hours=2)})
    intent = fusion().fuse(make_signal(strength=0.5), snap, Regime.BREAKOUT, opinion=old)
    assert intent is not None
    assert "ignored stale" in intent.ai_research_context


def test_fusion_illiquid_chain_no_intent(calendar) -> None:
    snap = make_verified_snapshot(calendar)
    thin_rows = tuple(
        OptionQuote(instrument=r.instrument, quote=r.quote, oi=1000.0)
        for r in snap.chain.rows
    )
    thin_chain = snap.chain.model_copy(update={"rows": thin_rows})
    thin_snap = snap.model_copy(update={"chain": thin_chain})
    assert fusion().fuse(make_signal(), thin_snap, Regime.BREAKOUT) is None


# --- committee -----------------------------------------------------------------


def _frame():
    bars = synth_day("2026-08-26", 3, 24_500.0, drift=0.0004)
    eng = FeatureEngine()
    cut = bars[40]
    return eng.compute("NIFTY", cut.ts, bars[:40])


def _queue_full_run(llm: FakeLLM, direction="LONG", action="FAVOR") -> None:
    llm.queue(TechnicalView, dict(
        trend_assessment="up", momentum_assessment="positive", volatility_assessment="normal",
        key_levels="24450/24600", bias=direction, confidence=0.7,
    ))
    llm.queue(ContextView, dict(
        news_risk="none known", sentiment_summary="no data", data_coverage="no news feed provided",
    ))
    llm.queue(DebateCase, dict(
        thesis="continuation", strongest_points=["trend", "vwap hold"],
        attack_on_opponent="bear ignores breadth", what_would_prove_me_wrong=["vwap loss"],
    ))
    llm.queue(DebateCase, dict(
        thesis="fade", strongest_points=["stretched"], attack_on_opponent="bull ignores IV",
        what_would_prove_me_wrong=["new highs"],
    ))
    for _ in range(3):
        llm.queue(RiskPersonaView, dict(stance="ok", hidden_risks=["expiry pin"],
                                        liquidity_concerns="", reward_risk_view="fair"))
    llm.queue(CommitteeDecision, dict(
        direction=direction, conviction=0.65, recommended_action=action,
        time_horizon="intraday", synthesis="balance favors longs",
        invalidation_conditions=["close below vwap", "pcr collapse"],
        confidence_quality=0.6,
    ))


def test_committee_produces_opinion(calendar) -> None:
    llm = FakeLLM()
    _queue_full_run(llm)
    committee = ResearchCommittee(deep=llm, quick=llm)
    snap = make_verified_snapshot(calendar)
    op = committee.run("NIFTY", snap, _frame(), Regime.TRENDING_UP, TRADING_TS)
    assert op is not None
    assert op.direction is Direction.LONG
    assert op.recommended_action is RecommendedAction.FAVOR
    assert op.invalidation_conditions
    assert op.contradicting_evidence  # bear's case preserved
    assert op.major_risks  # conservative's objections preserved
    assert op.snapshot_id == snap.snapshot_id
    # numbers shown to the LLM came only from the snapshot section
    facts_call = llm.calls[0][1]
    assert "VERIFIED MARKET SNAPSHOT" in facts_call


def test_committee_fails_closed_on_stage_failure(calendar) -> None:
    llm = FakeLLM()
    llm.queue(TechnicalView, dict(
        trend_assessment="up", momentum_assessment="x", volatility_assessment="y",
        key_levels="z", bias="LONG", confidence=0.7,
    ))
    llm.queue(ContextView, None)  # news stage fails schema/call
    committee = ResearchCommittee(deep=llm, quick=llm)
    snap = make_verified_snapshot(calendar)
    op = committee.run("NIFTY", snap, _frame(), Regime.TRENDING_UP, TRADING_TS)
    assert op is None  # no free-text fallback, no partial opinion


def test_memory_roundtrip(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite")
    op = make_opinion()
    store.store_opinion(op)
    assert store.research_context("NIFTY", "BREAKOUT") == ""  # unresolved: excluded
    store.resolve_opinion(op.opinion_id, pnl=450.0, note="worked")
    ctx = store.research_context("NIFTY", "BREAKOUT")
    assert "LONG" in ctx and "+450" in ctx
    stats = store.strategy_stats("orb_v1")
    assert stats["n"] == 0
    store.close()
