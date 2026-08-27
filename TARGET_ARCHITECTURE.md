# TARGET ARCHITECTURE — QFT Platform

Personal systematic trading platform for NSE index derivatives via Groww.
Capital: INR 50,000. Objective: maximize long-term, after-cost, risk-adjusted return while
minimizing probability of ruin, drawdown, overfitting, and execution failure.

Companion documents: [`ARCHITECTURE_AUDIT.md`](ARCHITECTURE_AUDIT.md) (what we take from
TradingAgents and why), [`docs/GROWW_API_REFERENCE.md`](docs/GROWW_API_REFERENCE.md) (verified
broker API surface), [`docs/RISK_POLICY.md`](docs/RISK_POLICY.md) (firewall rules).

---

## 1. Governing principle

```
LLMs PROPOSE.   DETERMINISTIC CODE AUTHORIZES.   BROKER ADAPTER EXECUTES.
```

Three consequences that shape everything below:

1. The AI research committee outputs `ResearchOpinion` — structured context. It cannot emit an
   order, a size, or a risk parameter. Fusion rules that consume opinions are themselves
   deterministic and backtested.
2. The risk firewall and execution daemon are small, boring, pure-Python, fully unit- and
   chaos-tested, and share **no process** with any LLM code.
3. Every hop between components is a typed, timestamped, persisted artifact
   (`VerifiedMarketSnapshot → Signal → TradeIntent → RiskDecision → OrderRequest → Fill → Position`),
   so every order is explainable and reproducible from the ledger alone.

## 2. System diagram

```
                          ┌────────────────────────────────────────────┐
                          │                 GROWW / NSE                │
                          └───────┬──────────────────────────▲─────────┘
                        market data│                          │orders (LIVE only)
                ┌──────────────────▼───────────┐    ┌─────────┴──────────────┐
                │  data/: GrowwReadOnlyAdapter │    │ execution/: daemon     │
                │  REST + WS feed, instruments │    │ GrowwExecutionAdapter  │
                │  point-in-time store         │    │ (sole secret holder)   │
                └──────────────────┬───────────┘    └─────────▲──────────────┘
                                   │ VerifiedMarketSnapshot   │ ApprovedOrder
                ┌──────────────────▼───────────┐    ┌─────────┴──────────────┐
                │ features/: FeatureEngine     │    │ risk/: RiskEngine      │
                │ regime/: RegimeEngine        │    │ deterministic firewall │
                └───────┬──────────────┬───────┘    │ kill switches          │
                        │              │            └─────────▲──────────────┘
        ┌───────────────▼──┐   ┌───────▼────────────┐         │ TradeIntent
        │ strategies/      │   │ research/ (agents) │         │
        │ deterministic    │   │ Claude committee   │  ┌──────┴─────────────┐
        │ quant strategies │   │ → ResearchOpinion  ├──► fusion/:           │
        └───────────────┬──┘   └───────┬────────────┘  │ SignalFusionEngine │
                        │ Signal       │ opinion       └────────────────────┘
                        └──────────────┴──────────────────────▲
                                                              │
   portfolio/: event-sourced ledger ◄── reconciliation/ ◄── broker truth
   monitoring/: dashboard + audit log across all components
```

All research components read from the same snapshot store; none can reach a broker adapter.

## 3. Module layout (`qft/` package)

```
qft/
  domain/          # Pydantic models: Instrument, OptionContract, Quote, Depth, Snapshot,
                   # Signal, TradeIntent, RiskDecision, OrderRequest/Ack/Status, Fill,
                   # Position, ResearchOpinion, Regime, enums, ids
  config/          # typed settings (pydantic-settings), risk.yaml schema, env pipeline
  data/            # BrokerDataProvider protocol; groww/ (REST+WS read-only adapter),
                   # instruments master, IST session & holiday calendar,
                   # point-in-time bar/chain store, snapshot validation (freshness, sanity)
  features/        # deterministic derivatives feature engine (timestamped FeatureFrame)
  regime/          # deterministic regime classifier
  strategies/      # Strategy interface, registry, concrete strategies (versioned)
  research/        # TradingAgents-descended committee on Claude → ResearchOpinion
                   # (LLM client factory, structured outputs, research memory, reflection)
  fusion/          # SignalFusionEngine: signals + regime + opinions → TradeIntent
  risk/            # RiskEngine firewall, kill switches, limits, event calendar
  backtest/        # event-driven intraday simulator, Indian cost model, walk-forward,
                   # Monte Carlo, robustness scoring, survival gates, promotion scoring
  execution/       # execution daemon: order state machine, idempotency, retries
  brokers/         # BrokerAdapter protocol; SimulatedBroker, PaperBroker,
                   # GrowwReadOnlyAdapter (account), GrowwExecutionAdapter
  portfolio/       # event-sourced ledger (SQLite WAL), positions, P&L, attribution
  reconciliation/  # broker-vs-ledger comparison, mismatch → HALT
  monitoring/      # structured logging, metrics, dashboard (FastAPI + HTMX/JSON)
  storage/         # SQLite/parquet persistence helpers, migrations
tests/             # unit, property-based (risk), integration, chaos
config/            # risk.yaml, strategies.yaml, environments.yaml (no secrets)
docs/
```

Boundaries enforced by import-linter contracts in CI: `research/` may import `domain/` and
`data/` read paths only; `risk/` and `execution/` may not import `research/` or any LLM client;
`brokers/groww_execution.py` is imported only by `execution/`.

## 4. Data layer

- **Primary source:** Groww Trade API (REST + NATS WebSocket) per
  `docs/GROWW_API_REFERENCE.md`. No yfinance in production paths.
- **Canonical instrument model:** `Instrument` (exchange, segment, trading_symbol, groww_symbol,
  exchange_token, underlying, expiry, strike, option_type, lot_size, tick_size, freeze_qty) built
  from the daily instruments master; lot sizes and expiries are always data, never constants.
- **Every message carries:** source, exchange timestamp (when provided), receive timestamp
  (UTC, tz-aware), instrument id, and a computed freshness.
- **`VerifiedMarketSnapshot`:** immutable, hashed object bundling underlying spot, futures quote,
  option chain slice, depth, and account state used for a decision. Verification = schema-valid +
  fresh within configured bounds + internal sanity (bid ≤ ask, non-negative OI, price within
  band vs. previous tick) + session-state consistency. Trading decisions may reference prices
  only from a verified snapshot; failure ⇒ NO TRADE (fail closed).
- **Point-in-time store:** append-only parquet/SQLite of bars, chains, OI snapshots with
  capture timestamps; the backtester and feature engine read through a PIT API that physically
  cannot serve data newer than the simulation clock (the TradingAgents "filter after fetch"
  pattern hardened into the storage interface).
- **IST session calendar:** NSE trading calendar, session windows, expiry days from the API,
  special sessions; all "is market open" logic lives here.

## 5. Feature & regime engines

Deterministic, pure functions of PIT data, output `FeatureFrame` keyed by (instrument,
timestamp): momentum, VWAP distance, ATR, realised/historical vol, IV + IV rank/percentile,
term structure slope, skew (25Δ proxy from chain), PCR (volume + OI), OI and ΔOI, futures basis,
breadth, opening-range state, gap context, trend strength, mean-reversion score, spread,
depth-based liquidity score, time-to-expiry, and position greeks when a chain is present.
No LLM anywhere in this path. Every feature records its input-window bounds to make leakage
audits mechanical.

`RegimeEngine` maps features → `Regime` enum (TRENDING_UP/DOWN, MEAN_REVERTING, LOW/HIGH_VOL,
BREAKOUT, EVENT_RISK, EXPIRY_REGIME, ILLIQUID, NO_TRADE) with hysteresis to prevent flapping.
Strategies declare allowed regimes; the scheduler runs only matching strategies.

## 6. Strategy framework

`Strategy` protocol: `strategy_id`, `version`, `allowed_instruments`, `allowed_regimes`,
`generate(features, snapshot, portfolio_state) -> Signal | None`, plus declared exit/stop/
time-exit rules, sizing rule (deterministic, risk-budget-based), liquidity requirements, cost
assumptions, and expected holding time. Strategies are stateless between bars except via
explicit persisted state. Initial research families: opening-range breakout, VWAP
trend-continuation, volatility-regime mean reversion, defined-risk directional option structures
(debit spreads). Structurally forbidden: naked short options, martingale, averaging down,
loss-triggered size increases.

## 7. Research committee (TradingAgents-descended, Claude)

Topology retained: analysts (technical, news, sentiment, macro when relevant) → bull/bear
adversarial debate → research manager → strategy analyst → aggressive/neutral/conservative risk
debate → committee synthesis. Runs on the LLM-factory pattern (Anthropic primary:
`claude-fable-5` deep tier / cheaper quick tier), structured outputs enforced — a failed schema
discards the opinion (**no free-text fallback**).

Output: `ResearchOpinion{instrument, timestamp, market_regime, direction, conviction ∈ [0,1],
time_horizon, supporting_evidence[], contradicting_evidence[], major_risks[],
invalidation_conditions[] (required, non-empty), news_risk, sentiment_context,
technical_context, recommended_action ∈ {favor, neutral, avoid}, confidence_quality,
data_quality}`. Bull/bear disagreement and the conservative analyst's objections are preserved
verbatim in the opinion. All numeric market facts injected from a `VerifiedMarketSnapshot`;
prompts forbid invented numbers and the snapshot injection makes it checkable.

Research memory (opinions + realized outcomes) and trading memory (per-trade quantitative
records) live in SQLite; retrieval by regime/setup similarity; reflection after trade close.
Memory can propose hypotheses; it has no write path to strategies, fusion rules, or risk config.

## 8. Signal fusion → TradeIntent

`SignalFusionEngine` is a deterministic rule table (versioned, backtested): inputs are the quant
`Signal`, regime, microstructure/liquidity checks, volatility state, portfolio state, and any
fresh `ResearchOpinion` for the underlying. Opinion effects are bounded: may scale conviction
within backtested bands, or veto (e.g., conservative-analyst red flags, event risk). It can never
raise size above the strategy's own sizing rule and never creates intents without a quant signal.
Output is a fully-specified `TradeIntent` (see `docs/RISK_POLICY.md` §validation for the field
contract) with deterministic `intent_id` = hash(strategy, instrument, signal timestamp, params).

## 9. Risk firewall

See `docs/RISK_POLICY.md` — the 12-step validation sequence, limits for ₹50k, kill switches,
fail-closed doctrine, and the lot-size rule (if minimum lot size exceeds the risk budget, the
trade does not happen). Implementation notes: pure functions over
(intent, snapshot, portfolio state, limits) → `RiskDecision` with exhaustive reason codes;
property-based tests assert invariants like "no approval can ever exceed daily-loss headroom";
config schema rejects out-of-range values at startup.

## 10. Execution

- `BrokerAdapter` protocol: `place`, `modify`, `cancel`, `order_status`,
  `order_status_by_reference`, `trades_for_order`, `positions`, `margins`.
- `SimulatedBroker` (backtest: fills from bar/quote data + cost model),
  `PaperBroker` (live data, simulated fills incl. partial-fill and rejection injection),
  `GrowwReadOnlyAdapter` (account/positions/margins, no order methods),
  `GrowwExecutionAdapter` (LIVE only, inside the daemon).
- Daemon loop: receives signed `ApprovedOrder`, re-validates risk + freshness, submits with
  deterministic `order_reference_id` (from intent id), persists `submitted` **before** the network
  call, resolves timeouts via status-by-reference before any retry (never blind-resubmits),
  tracks the state machine `created → submitted → acked → {partially_filled* → filled} |
  rejected | cancelled`, records fills to the ledger, enforces order-frequency caps.
- Broker positions are the source of truth: on restart the daemon reconciles before accepting
  any new order; unexplained mismatch ⇒ HALT + alert.

## 11. Ledger, attribution, memory

Event-sourced SQLite (WAL) ledger: every TradeIntent, RiskDecision, OrderRequest, ack, status
transition, fill, fee/tax computation, position change, snapshot hash, and risk event, with
immutable ids and UTC timestamps. Post-trade attribution decomposes P&L into strategy alpha,
market move, execution quality (slippage vs. arrival), AI contribution (fusion adjustment
delta), and costs. LLM reflections reference ledger rows; they can never modify them.

## 12. Environments & promotion

`BACKTEST → PAPER → SHADOW → LIVE` selected by explicit config + CLI flag; the environment is
stamped into every ledger row.

- Promotion gates (objective, per strategy): Stage-A survival gates (no leakage, no risk
  violations, realistic fills, drawdown/margin bounds, sample size, OOS behavior, parameter
  stability) then Stage-B score = 0.30·OOS Sortino + 0.25·Calmar + 0.15·walk-forward consistency
  + 0.10·after-cost expectancy + 0.10·CVaR quality + 0.10·parameter robustness, minus explicit
  penalties (drawdown, turnover, costs, tail skew, instability, frequency, correlation,
  concentration, regime dependence, OOS degradation). All returns are net of brokerage, STT,
  exchange charges, SEBI fees, GST, stamp duty, and modeled slippage.
- PAPER ≥ 20 trading days and SHADOW ≥ 10 days with live-vs-model tracking error within bounds
  before LIVE eligibility; automatic demotion on breach.
- LIVE arming: `qft arm-live` requires typed confirmation phrase + writes an arming record with
  expiry; **any restart disarms**; the daemon refuses orders unless armed AND environment==LIVE
  AND all kill switches clear. Default state is disarmed, always.

## 13. Observability

FastAPI dashboard (read-only view over ledger + live state): capital, cash, margin,
realised/unrealised P&L, drawdown vs. HWM, open positions, pending orders, per-strategy P&L and
status, current regime, recent signals and rejected intents with reason codes, latest
ResearchOpinion vs. quant confidence, feed freshness, execution latency, slippage distribution,
kill-switch and environment/armed state. Structured JSON logs with correlation ids
(intent_id → order_reference_id → groww_order_id); log scrubbing filter guarantees tokens/keys
never appear in logs (the upstream key-in-URL leak class is tested against).

## 14. Engineering standards

Python 3.12, fully typed (mypy strict on `qft/domain`, `qft/risk`, `qft/execution`; standard
elsewhere), Pydantic v2 models for all contracts, dependency injection at composition roots (no
globals), `uv` with committed lockfile, Ruff (lint+format) in CI, pytest with unit /
property-based (Hypothesis, risk engine) / integration / chaos markers, broker mocks, GitHub
Actions gate. Pinned upstream: TradingAgents `a33fd4c0` (conceptual reference only, vendored
nothing), `growwapi==1.5.0` as the API contract reference. No credentials anywhere in the repo;
secrets only via environment injection into the specific process that needs them.

## 15. Delivery order

1. Domain models + config + calendars (foundation)
2. Cost model (Indian F&O cost stack) — needed by backtest and risk alike
3. Data layer: Groww read-only adapter + validation + PIT store (SimulatedFeed for tests)
4. Feature + regime engines
5. Backtester + survival gates + scoring
6. Strategy interface + first strategy family (opening-range breakout, defined-risk variants)
7. Risk engine + kill switches (property-tested)
8. Simulated/Paper brokers + execution daemon + ledger + reconciliation
9. Research committee (`ResearchOpinion`) + fusion engine
10. Dashboard + audit logs
11. Chaos suite; then backtests → walk-forward → paper → shadow → GO_LIVE_READINESS_REPORT.md
12. STOP. Live activation is a human decision, out of scope for the software.
