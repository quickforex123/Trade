"""SignalFusionEngine: deterministic rule table combining a quant Signal with
regime, liquidity, and (optionally) a fresh ResearchOpinion into a TradeIntent.

Bounded AI influence, by construction:
- An opinion can only scale conviction within [veto, dampen, neutral, boost]
  bands that are themselves configuration — it can never raise size above the
  strategy's own sizing rule (sizing happens here from the risk budget and is
  capped by config), and it can never create an intent without a quant signal.
- A missing/stale/low-quality opinion contributes nothing (and, if
  require_research_opinion is set, vetoes).

Instrument selection policy (intraday, defined-risk): directional views are
expressed as LONG premium — buy CE for LONG, buy PE for SHORT — nearest
liquid expiry, strike nearest to a configured delta proxy (ATM by default).
Short-option structures are out of scope until the risk engine's margin gates
are extended (allow_short_options=false structurally).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from qft.config.risk_limits import RiskLimits
from qft.costs.model import CostModel
from qft.domain.enums import Direction, OptionType, OrderType, RecommendedAction, Regime, Side
from qft.domain.ids import deterministic_id
from qft.domain.market import OptionQuote, VerifiedMarketSnapshot
from qft.domain.research import ResearchOpinion
from qft.domain.signals import Signal, TradeIntent
from qft.domain.time import IST, ensure_utc

logger = logging.getLogger(__name__)


class FusionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_signal_strength: float = Field(ge=0, le=1, default=0.3)
    min_fused_conviction: float = Field(ge=0, le=1, default=0.35)
    opinion_max_age_seconds: float = Field(gt=0, default=1800)
    opinion_boost: float = Field(ge=1.0, le=1.5, default=1.15)
    opinion_dampen: float = Field(ge=0.3, le=1.0, default=0.7)
    conservative_veto_conviction: float = Field(ge=0, le=1, default=0.75)
    signal_ttl_seconds: float = Field(gt=0, default=20)
    target_delta: float = Field(gt=0, le=1, default=0.5)  # ATM proxy
    stop_fraction_of_premium: float = Field(gt=0, le=1, default=0.30)
    reward_multiple: float = Field(gt=0, default=2.0)


class SignalFusionEngine:
    def __init__(
        self,
        limits: RiskLimits,
        cost_model: CostModel,
        config: FusionConfig | None = None,
    ) -> None:
        self._limits = limits
        self._costs = cost_model
        self._cfg = config or FusionConfig()

    def fuse(
        self,
        signal: Signal,
        snapshot: VerifiedMarketSnapshot,
        regime: Regime,
        opinion: ResearchOpinion | None = None,
        now: datetime | None = None,
    ) -> TradeIntent | None:
        """Return a TradeIntent or None. None is a positive no-trade decision."""
        now = ensure_utc(now) if now else signal.ts

        if signal.direction is Direction.NEUTRAL:
            return None
        if signal.strength < self._cfg.min_signal_strength:
            return None
        if not snapshot.verified:
            logger.info("fusion: snapshot unverified — no intent")
            return None
        if regime in (Regime.NO_TRADE, Regime.EVENT_RISK, Regime.ILLIQUID):
            return None

        conviction, ai_context = self._apply_opinion(signal, opinion, now)
        if conviction is None:
            return None
        if conviction < self._cfg.min_fused_conviction:
            logger.info("fusion: conviction %.2f below floor — no intent", conviction)
            return None

        leg = self._select_option(signal, snapshot)
        if leg is None:
            logger.info("fusion: no liquid option leg — no intent")
            return None
        instrument, quote = leg.instrument, leg.quote

        ask = quote.ask or quote.ltp
        if ask <= 0:
            return None
        limit_price = instrument.ceil_to_tick(ask)  # marketable: never below the ask
        lots = 1  # sizing floor; the risk engine enforces budgets and can only reject
        quantity = lots * instrument.lot_size

        stop_points = round(limit_price * self._cfg.stop_fraction_of_premium, 2)
        est_cost = self._costs.option_round_trip(
            Side.BUY, limit_price, limit_price * (1 - self._cfg.stop_fraction_of_premium), quantity
        )
        est_max_loss = stop_points * quantity + est_cost
        expected_reward = stop_points * self._cfg.reward_multiple * quantity - est_cost

        ist = now.astimezone(IST)
        square_off = ist.replace(
            hour=self._limits.session.square_off_ist.hour,
            minute=self._limits.session.square_off_ist.minute,
            second=0,
            microsecond=0,
        )

        option_type = OptionType.CE if signal.direction is Direction.LONG else OptionType.PE
        return TradeIntent(
            intent_id=deterministic_id("int", signal.signal_id, instrument.key),
            strategy_id=signal.strategy_id,
            strategy_version=signal.strategy_version,
            ts=now,
            signal_expiry=now + timedelta(seconds=self._cfg.signal_ttl_seconds),
            underlying=signal.underlying,
            instrument_key=instrument.key,
            expiry=instrument.expiry,
            strike=instrument.strike,
            option_type=option_type,
            side=Side.BUY,  # defined-risk long premium only
            lots=lots,
            quantity=quantity,
            entry_type=OrderType.LIMIT,
            entry_price_limit=limit_price,
            max_slippage_pct=min(self._limits.max_slippage_pct, 0.015),
            stop_condition=f"premium <= {instrument.round_to_tick(limit_price - stop_points)}",
            stop_loss_points=stop_points,
            target_condition=(
                f"premium >= {instrument.round_to_tick(limit_price + stop_points * self._cfg.reward_multiple)}"
            ),
            time_exit_utc=square_off.astimezone(now.tzinfo),
            estimated_transaction_cost=round(est_cost, 2),
            estimated_max_loss=round(est_max_loss, 2),
            expected_reward=round(max(expected_reward, 0.0), 2),
            quant_confidence=round(conviction, 4),
            ai_research_context=ai_context,
            market_regime=regime,
            snapshot_id=snapshot.snapshot_id,
            reason_code=signal.rationale[:80] or signal.strategy_id.upper(),
        )

    # -- opinion handling --------------------------------------------------------

    def _apply_opinion(
        self, signal: Signal, opinion: ResearchOpinion | None, now: datetime
    ) -> tuple[float | None, str]:
        """Returns (fused conviction | None if vetoed, audit context string)."""
        base = signal.strength
        if opinion is None:
            if self._limits.require_research_opinion:
                return None, "vetoed: research opinion required but absent"
            return base, "none"

        age = (now - opinion.ts).total_seconds()
        if age > self._cfg.opinion_max_age_seconds or opinion.data_quality.value == "BAD":
            if self._limits.require_research_opinion:
                return None, f"vetoed: opinion stale/bad ({opinion.opinion_id})"
            return base, f"ignored stale/bad opinion {opinion.opinion_id}"

        aligned = (
            (signal.direction is Direction.LONG and opinion.direction is Direction.LONG)
            or (signal.direction is Direction.SHORT and opinion.direction is Direction.SHORT)
        )
        opposed = (
            (signal.direction is Direction.LONG and opinion.direction is Direction.SHORT)
            or (signal.direction is Direction.SHORT and opinion.direction is Direction.LONG)
        )

        # Hard veto: committee says AVOID with high conviction.
        if opinion.recommended_action is RecommendedAction.AVOID and (
            opinion.conviction >= self._cfg.conservative_veto_conviction
        ):
            return None, f"vetoed by {opinion.opinion_id} (AVOID c={opinion.conviction:.2f})"

        if opposed and opinion.conviction >= self._cfg.conservative_veto_conviction:
            return None, f"vetoed by {opinion.opinion_id} (opposed c={opinion.conviction:.2f})"

        if aligned and opinion.recommended_action is RecommendedAction.FAVOR:
            fused = min(1.0, base * self._cfg.opinion_boost)
            return fused, f"boosted by {opinion.opinion_id} ({base:.2f}->{fused:.2f})"
        if opposed or opinion.recommended_action is RecommendedAction.AVOID:
            fused = base * self._cfg.opinion_dampen
            return fused, f"dampened by {opinion.opinion_id} ({base:.2f}->{fused:.2f})"
        return base, f"neutral opinion {opinion.opinion_id}"

    # -- instrument selection ----------------------------------------------------

    def _select_option(
        self, signal: Signal, snapshot: VerifiedMarketSnapshot
    ) -> OptionQuote | None:
        chain = snapshot.chain
        if chain is None or not chain.rows:
            return None
        want = OptionType.CE if signal.direction is Direction.LONG else OptionType.PE
        spot = chain.underlying_price
        candidates: list[OptionQuote] = []
        for row in chain.rows:
            inst = row.instrument
            if inst.option_type is not want or not inst.strike:
                continue
            q = row.quote
            spread = q.spread_pct
            if spread is None or spread > self._limits.max_spread_pct_options:
                continue
            oi = row.oi if row.oi is not None else q.open_interest
            if oi is None or oi < self._limits.min_open_interest:
                continue
            if (q.ask or 0) <= 0:
                continue
            candidates.append(row)
        if not candidates:
            return None

        def strike_score(row: OptionQuote) -> float:
            if row.delta is not None:
                return abs(abs(row.delta) - self._cfg.target_delta)
            assert row.instrument.strike is not None
            return abs(row.instrument.strike - spot) / max(spot, 1e-9)

        return min(candidates, key=strike_score)
