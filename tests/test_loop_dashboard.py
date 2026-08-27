"""End-to-end: TradingLoop wiring (PAPER fills, SHADOW never submits) and the
read-only dashboard."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from qft.brokers.simulated import SimulatedBroker
from qft.config.risk_limits import RiskLimits
from qft.costs.model import CostModel
from qft.domain.enums import Direction, Environment, Regime
from qft.domain.ids import deterministic_id
from qft.domain.market import VerifiedMarketSnapshot
from qft.domain.portfolio import LedgerEventType
from qft.domain.signals import Signal
from qft.execution.daemon import ExecutionDaemon
from qft.features.engine import FeatureEngine
from qft.fusion.engine import SignalFusionEngine
from qft.monitoring.dashboard import create_dashboard
from qft.portfolio.ledger import Ledger
from qft.reconciliation.service import Reconciler
from qft.regime.engine import RegimeEngine
from qft.risk.engine import RiskEngine
from qft.risk.kill_switch import KillSwitchManager
from qft.runtime.loop import TradingLoop
from qft.strategies.base import Strategy, StrategySpec
from qft.strategies.registry import StrategyRegistry
from tests.conftest import TRADING_TS
from tests.synthetic import synth_day
from tests.test_fusion_research import make_verified_snapshot

pytestmark = pytest.mark.integration

LIMITS = RiskLimits(initial_capital=50_000)


class AlwaysLong(Strategy):
    """Test stub: emits one LONG signal per cycle."""

    def __init__(self) -> None:
        self.spec = StrategySpec(
            strategy_id="stub_long",
            version="0",
            allowed_regimes=(Regime.BREAKOUT, Regime.TRENDING_UP, Regime.NO_TRADE,
                             Regime.LOW_VOLATILITY, Regime.MEAN_REVERTING,
                             Regime.HIGH_VOLATILITY, Regime.TRENDING_DOWN),
        )

    def generate(self, frame, regime, snapshot):
        return Signal(
            signal_id=deterministic_id("sig", "stub", frame.as_of.isoformat()),
            strategy_id=self.spec.strategy_id,
            strategy_version="0",
            ts=frame.as_of,
            underlying="NIFTY",
            direction=Direction.LONG,
            strength=0.8,
            regime=regime,
            rationale="STUB",
        )


def _frame():
    bars = synth_day("2026-08-26", 3, 24_500.0, drift=0.0004)
    return FeatureEngine().compute("NIFTY", TRADING_TS, [b for b in bars if b.ts < TRADING_TS])


def build_loop(tmp_path, calendar, env: Environment, snapshot: VerifiedMarketSnapshot):
    registry = StrategyRegistry()
    registry.register(AlwaysLong())
    ledger = Ledger(tmp_path / "ledger.sqlite", env, 50_000)
    ks = KillSwitchManager()
    risk = RiskEngine(
        LIMITS, calendar, ks, env,
        allowed_expiries_provider=lambda: [TRADING_TS.date().replace(day=1, month=9)],
    )
    broker = SimulatedBroker()
    # feed the sim broker the chain quotes so limit orders can fill
    for row in snapshot.chain.rows:
        broker.set_quote(row.instrument.trading_symbol, row.quote)
    daemon = ExecutionDaemon(broker, ledger) if env is not Environment.SHADOW else None
    reconciler = Reconciler(broker, ledger, ks)
    instruments = {r.instrument.trading_symbol: r.instrument for r in snapshot.chain.rows}
    loop = TradingLoop(
        environment=env,
        registry=registry,
        regime_engine=RegimeEngine(hysteresis_bars=1),
        fusion=SignalFusionEngine(LIMITS, CostModel()),
        risk=risk,
        daemon=daemon,
        ledger=ledger,
        reconciler=reconciler,
        limits=LIMITS,
        instrument_lookup=instruments,
    )
    return loop, ledger, ks, broker


def test_paper_cycle_full_pipeline(tmp_path, calendar) -> None:
    snap = make_verified_snapshot(calendar)
    loop, ledger, ks, broker = build_loop(tmp_path, calendar, Environment.PAPER, snap)
    result = loop.run_cycle(snap, _frame(), TRADING_TS)
    assert result.signals == 1
    assert result.intents == 1, result.reasons
    assert result.approved == 1, result.reasons
    assert result.submitted == 1
    fills = ledger.fills()
    assert len(fills) == 1
    assert fills[0].quantity == 65
    # complete audit trail exists
    types = {e["type"] for e in ledger.events()}
    assert {"SNAPSHOT", "TRADE_INTENT", "RISK_DECISION", "ORDER_APPROVED",
            "ORDER_REQUEST", "FILL"} <= types


def test_second_cycle_blocked_by_exposure(tmp_path, calendar) -> None:
    snap = make_verified_snapshot(calendar)
    loop, ledger, ks, broker = build_loop(tmp_path, calendar, Environment.PAPER, snap)
    loop.run_cycle(snap, _frame(), TRADING_TS)
    r2 = loop.run_cycle(snap, _frame(), TRADING_TS + timedelta(minutes=5))
    assert r2.approved == 0
    assert r2.rejected >= 1  # concurrent-position / underlying-exposure gates


def test_shadow_never_submits(tmp_path, calendar) -> None:
    snap = make_verified_snapshot(calendar)
    loop, ledger, ks, broker = build_loop(tmp_path, calendar, Environment.SHADOW, snap)
    result = loop.run_cycle(snap, _frame(), TRADING_TS)
    assert result.approved == 1, result.reasons
    assert result.submitted == 0
    assert ledger.fills() == []
    assert ledger.events(LedgerEventType.ORDER_REQUEST) == []
    # but the decision trail is fully recorded for shadow evaluation
    assert ledger.events(LedgerEventType.RISK_DECISION)


def test_dashboard_endpoints(tmp_path, calendar) -> None:
    snap = make_verified_snapshot(calendar)
    loop, ledger, ks, broker = build_loop(tmp_path, calendar, Environment.PAPER, snap)
    loop.run_cycle(snap, _frame(), TRADING_TS)
    app = create_dashboard(ledger, ks, Environment.PAPER, 50_000)
    client = TestClient(app)
    state = client.get("/api/state").json()
    assert state["environment"] == "PAPER"
    assert state["kill_switch"] == "NONE"
    assert state["equity"] > 0
    assert state["open_positions"]
    html_page = client.get("/").text
    assert "QFT — PAPER" in html_page
    events = client.get("/api/events", params={"event_type": "FILL"}).json()
    assert len(events) == 1
