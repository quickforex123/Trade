"""The trading loop: one deterministic pass per decision cycle.

    snapshot -> features -> regime -> strategies -> fusion -> RISK -> execution

Used by PAPER (simulated fills on live data), SHADOW (everything real except
order placement — intents and decisions are recorded, orders are NOT sent),
and LIVE (armed daemon). BACKTEST uses qft.backtest instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from qft.config.risk_limits import RiskLimits
from qft.domain.enums import Environment, OrderType, Product, Side, Validity
from qft.domain.instruments import Instrument
from qft.domain.market import VerifiedMarketSnapshot
from qft.domain.orders import ApprovedOrder
from qft.domain.portfolio import LedgerEventType
from qft.domain.research import ResearchOpinion
from qft.domain.signals import TradeIntent
from qft.domain.time import ensure_utc
from qft.execution.daemon import ExecutionDaemon
from qft.features.engine import FeatureFrame
from qft.fusion.engine import SignalFusionEngine
from qft.portfolio.ledger import Ledger
from qft.reconciliation.service import Reconciler
from qft.regime.engine import RegimeEngine
from qft.risk.engine import RiskEngine
from qft.strategies.registry import StrategyRegistry

logger = logging.getLogger(__name__)


@dataclass
class CycleResult:
    signals: int = 0
    intents: int = 0
    approved: int = 0
    rejected: int = 0
    submitted: int = 0
    reasons: tuple[str, ...] = ()


class TradingLoop:
    def __init__(
        self,
        environment: Environment,
        registry: StrategyRegistry,
        regime_engine: RegimeEngine,
        fusion: SignalFusionEngine,
        risk: RiskEngine,
        daemon: ExecutionDaemon | None,
        ledger: Ledger,
        reconciler: Reconciler | None,
        limits: RiskLimits,
        instrument_lookup: dict[str, Instrument],
    ) -> None:
        if environment is Environment.SHADOW and daemon is not None:
            raise ValueError("SHADOW must not carry an execution daemon — zero order placement")
        self._env = environment
        self._registry = registry
        self._regimes = regime_engine
        self._fusion = fusion
        self._risk = risk
        self._daemon = daemon
        self._ledger = ledger
        self._reconciler = reconciler
        self._limits = limits
        self._instruments = instrument_lookup

    def run_cycle(
        self,
        snapshot: VerifiedMarketSnapshot,
        frame: FeatureFrame,
        now: datetime,
        opinion: ResearchOpinion | None = None,
    ) -> CycleResult:
        now = ensure_utc(now)
        result = CycleResult()
        reasons: list[str] = []

        self._ledger.append(
            LedgerEventType.SNAPSHOT,
            {"snapshot_id": snapshot.snapshot_id, "verified": snapshot.verified,
             "quality": snapshot.data_quality.value, "issues": list(snapshot.issues)},
        )
        if opinion is not None:
            self._ledger.append(LedgerEventType.RESEARCH_OPINION, opinion.model_dump(mode="json"))

        reconciled = True
        if self._reconciler is not None:
            reconciled = self._reconciler.run().reconciled

        regime = self._regimes.classify(frame)

        open_positions = [p for p in self._ledger.positions().values() if not p.is_flat]
        open_symbols = {p.trading_symbol for p in open_positions}
        open_underlyings = tuple(
            inst.underlying
            for sym, inst in self._instruments.items()
            if sym in open_symbols and inst.underlying
        )
        portfolio = self._ledger.portfolio_view(
            now,
            reconciled=reconciled,
            open_underlyings=open_underlyings,
        )

        for strategy in self._registry.all():
            signal = strategy.generate(frame, regime, snapshot)
            if signal is None:
                continue
            result.signals += 1

            intent = self._fusion.fuse(signal, snapshot, regime, opinion=opinion, now=now)
            if intent is None:
                reasons.append(f"{strategy.spec.strategy_id}: fusion declined")
                continue
            result.intents += 1
            self._ledger.append(
                LedgerEventType.TRADE_INTENT, intent.model_dump(mode="json"),
                intent_id=intent.intent_id,
            )

            instrument = self._instrument_for(intent)
            decision = self._risk.evaluate(intent, snapshot, portfolio, instrument, now)
            self._ledger.append(
                LedgerEventType.RISK_DECISION, decision.model_dump(mode="json"),
                intent_id=intent.intent_id,
            )
            if not decision.approved:
                result.rejected += 1
                reasons.append(
                    f"{intent.intent_id}: rejected {[r.value for r in decision.reasons]}"
                )
                continue
            result.approved += 1

            if self._env is Environment.SHADOW or self._daemon is None:
                reasons.append(f"{intent.intent_id}: approved (shadow — not submitted)")
                continue

            order = self._to_order(intent, decision.decision_id)
            self._ledger.append(
                LedgerEventType.ORDER_APPROVED, order.model_dump(mode="json"),
                intent_id=intent.intent_id, order_ref=order.order_reference_id,
            )
            self._risk.record_order_submitted(order.order_reference_id, now)
            status = self._daemon.submit(order, now=now)
            result.submitted += 1
            if status.state.value in ("FILLED", "REJECTED", "CANCELLED"):
                self._risk.record_order_terminal(order.order_reference_id)

        result.reasons = tuple(reasons)
        return result

    def _instrument_for(self, intent: TradeIntent) -> Instrument | None:
        symbol = intent.instrument_key.split(":")[-1]
        return self._instruments.get(symbol)

    def _to_order(self, intent: TradeIntent, decision_id: str) -> ApprovedOrder:
        return ApprovedOrder(
            # Deterministic reference: one intent -> exactly one order id, ever.
            order_reference_id=intent.intent_id.replace("int_", "qo")[:20],
            intent_id=intent.intent_id,
            decision_id=decision_id,
            ts=intent.ts,
            exchange="NSE",
            segment="FNO",
            trading_symbol=intent.instrument_key.split(":")[-1],
            side=Side(intent.side),
            quantity=intent.quantity,
            order_type=OrderType(intent.entry_type),
            product=Product.MIS,
            validity=Validity.DAY,
            price=intent.entry_price_limit,
            expires_at=intent.signal_expiry + timedelta(seconds=10),
        )
