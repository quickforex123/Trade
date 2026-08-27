# ARCHITECTURE AUDIT — TauricResearch/TradingAgents

**Audited revision:** `a33fd4c0f134485a43553a2c23a63cb14adbd88f` (v0.3.1, 2026-07-18) — this is the
**pinned upstream revision** for this project. Upstream changes are never auto-merged.

**Verdict in one paragraph:** TradingAgents is a well-engineered *advisory research pipeline* for
US cash equities and crypto, producing markdown reports and a 5-tier rating string. Its multi-agent
debate architecture, structured-output discipline, checkpointing, look-ahead guards and test
culture are genuinely reusable. Its data layer, instrument model, "risk management" and
memory/reflection layers are unusable for Indian intraday F&O: data is daily-OHLCV-only with zero
derivatives primitives, risk control is LLM prose with no enforcement, position sizing does not
exist, and nothing ever reconciles against a broker because nothing ever executes. We therefore
adopt TradingAgents as the **conceptual blueprint for the intelligence layer only**, reimplemented
against our own domain model — not forked as the platform core.

---

## 1. What the system actually is

### 1.1 Orchestration (LangGraph)

`StateGraph(AgentState)` built in `graph/setup.py:61-156`, compiled in `trading_graph.py:149-150`.
Flow: selected analysts in sequence (each with a tool loop: agent → tools → agent until no tool
calls, then a message-clear node) → Bull/Bear debate → Research Manager → Trader →
Aggressive/Conservative/Neutral risk debate → Portfolio Manager → END.

- Debate loop: deterministic counters; terminates at `2 × max_debate_rounds` (bull/bear) and
  `3 × max_risk_discuss_rounds` (risk trio); default 1 round each (`conditional_logic.py:52-73`).
- Routing crash-safety: every conditional edge shares the complete path map so router
  fall-through can't crash the graph (`setup.py:32-42`, upstream #1088).
- State: `AgentState(MessagesState)` — 15 mostly-string fields; only `messages` has a reducer,
  everything else is last-write-wins (`agents/utils/agent_states.py:47-77`).

### 1.2 Outputs

`propagate()` returns `(final_state, rating)` where rating ∈ {Buy, Overweight, Hold, Underweight,
Sell}, extracted from Portfolio Manager prose by regex (`agents/utils/rating.py:28-48` — defaults
to `Hold` on parse failure). Structured outputs exist for Research Manager (`ResearchPlan`),
Trader (`TraderProposal`: action, reasoning, optional entry/stop/sizing *string*), Portfolio
Manager (`PortfolioDecision`), Sentiment Analyst (`SentimentReport`, 0–10 score bounds enforced).
Structured invocation is **one attempt, then free-text fallback** with all schema guarantees lost
(`agents/utils/structured.py:59-89`).

### 1.3 Checkpointing

Opt-in SQLite (`langgraph-checkpoint-sqlite`), one DB per ticker, thread id =
`sha256(ticker:date:analysts|debate|risk|asset)[:16]` so a resume under a different graph shape
starts fresh (`graph/checkpointer.py:28-38`, upstream #1089). Checkpoint rows cleared on success.
Caveat found: on resume the initial state is re-applied over recovered channels; only `messages`
has a reducer, so recovered reports can be clobbered — acceptable for research, not for anything
transactional.

### 1.4 Memory & reflection

No vector store (the ChromaDB design was removed upstream). A single append-only markdown file
(`agents/utils/memory.py`): entries `[date | ticker | rating | pending]` → resolved with realized
raw/alpha returns computed via yfinance in the *next* run (`trading_graph.py:296-334`), plus a
2-4 sentence LLM reflection. Retrieval is recency-based (last 5 same-ticker + 3 cross-ticker),
injected only into the Portfolio Manager prompt. No locking; no semantic similarity.

### 1.5 Data layer

Registry-routed vendors (`dataflows/interface.py:36-144`): yfinance, Alpha Vantage, FRED,
Polymarket (+ Reddit/StockTwits for sentiment). Ordered fallback chains per category with typed
errors (`NoMarketDataError`, `VendorRateLimitError`, `VendorNotConfiguredError`).

Genuinely good controls (worth copying as *patterns*):
- Look-ahead cut on OHLCV: `df[Date <= curr_date]` applied post-download and re-applied in the
  verifier (`stockstats_utils.py:215`, `market_data_validator.py:42`).
- News window `[start, end+1d)` with UTC normalization; undated articles excluded from
  historical windows (`yfinance_news.py:72-84`).
- Stale-OHLCV guard: > 10 calendar days ⇒ typed `NoMarketDataError` (`stockstats_utils.py:94-128`).
- Same-day cache TTL 900 s with explicit partial-candle reasoning (`stockstats_utils.py:131-145`).
- Path-traversal-hardened cache keys (`dataflows/utils.py:17-42`).
- Verified snapshot for anti-hallucination: deterministic OHLCV + 11 stockstats indicators
  rendered with a "treat as source of truth" contract (`market_data_validator.py`).

Structural limits (not fixable by configuration):
- **Daily bars only.** No `interval` parameter exists on any code path; indicators are computed on
  daily closes; the current-day bar is a knowingly partial candle behind a 15-minute TTL.
- **Zero derivatives primitives.** No strike/expiry/OI/IV/greeks/chain/basis/lot-size anywhere;
  the instrument model is a bare ticker string with `asset_type ∈ {stock, crypto}`.
- **No bid/ask, depth, or any executability concept.** No slippage or liquidity model.
- "Verified" means self-consistent with the yfinance daily cache — no cross-vendor check, no
  freshness disclosure in the rendered snapshot.
- Fundamentals ignore `curr_date` (live TTM data ⇒ look-ahead in backtests); point-in-time news is
  *safe* but effectively empty for historical dates (fetch-latest-then-filter).
- US-centric everything: FRED macro, Fed/S&P news queries, wallstreetbets sentiment, no NSE
  vocabulary (`NIFTY` doesn't even resolve as a symbol), no IST/session/holiday calendar,
  `datetime.now()` is host-local naive.

### 1.6 Execution & risk — the decisive findings

- **Nothing places orders.** Repo-wide search confirms no broker integration of any kind. The
  terminal artifacts are markdown + a rating string. (Good: nothing dangerous to remove; also
  means execution assumptions were never battle-tested.)
- **Position sizing does not exist.** `TraderProposal.position_sizing` is an optional free-text
  string; there is no capital base, portfolio state, or exposure computation anywhere.
- **"Risk management" is three personas of prose.** Aggressive/Conservative/Neutral debaters
  produce context for the Portfolio Manager; there is no code path by which any risk objection
  can block or modify anything.
- Stop-loss/entry/target fields are rendered to markdown and never validated (a Buy with a stop
  above entry passes) nor consumed.
- The three decision outputs (ResearchPlan / TraderProposal / PortfolioDecision) are never
  cross-checked for consistency.

### 1.7 LLM client layer

Clean factory abstraction (`llm_clients/factory.py`): native Anthropic/Google/Azure/Bedrock +
any OpenAI-compatible endpoint; two-tier deep/quick model split; `anthropic_effort` support with
model gating (Fable 5 / `claude-fable-5` explicitly in the deep-think catalog,
`model_catalog.py:95-106`); content-block normalization for Claude; per-provider retry budget via
`llm_max_retries`. No application-level timeout/circuit breaker around graph nodes.

### 1.8 Tests, CI, security

**Tests:** 67 files / 452 test functions; an unusually disciplined, bug-driven regression suite —
nearly every file cites the upstream issue it regresses. Dense coverage of the data layer
(vendor routing, look-ahead, stale/empty data, symbol normalization, path traversal) and of
LLM-provider quirks. Gaps: no end-to-end `propagate()` test, the 280-line CLI run path is
untested, checkpoint tests use a synthetic graph (which let a real bug ship — the CLI bypasses
`propagate()` entirely, silently disabling checkpointing, memory injection and reflection), no
coverage measurement.

**CI:** one workflow: pytest on 3.10–3.13, a smoke install-and-import job, `ruff check .`
(E,W,F,I,B,UP,C4,SIM). No type checker anywhere (annotations are decorative), no format check,
no coverage gate, no security scanning.

**Dependencies:** weakest area — 23 runtime deps all unbounded `>=`, no lockfile of any kind, and
two entirely dead core dependencies (`backtrader`, `redis` — declared, never imported).

**Secrets:** `.env`-based, properly gitignored, masked interactive capture. Two real leakage bugs
found: FRED and Alpha Vantage put the API key in the URL query string, and `HTTPError` strings
(which include the full URL) flow into warning logs and even into LLM-visible tool output for
"optional" categories — a key can end up inside a saved markdown report. Also: the CLI makes an
unconditional GET to a third-party announcements endpoint with server-controlled blocking UI.

**Execution code: none** (confirmed by exhaustive search). The README's claim that "the order will
be sent to the simulated exchange and executed" is stale/aspirational — no such component exists.

**Lessons imported into our platform:** typed + type-checked code with `mypy`/`pyright` in CI;
locked dependencies (`uv.lock`); no secrets in URLs (and log scrubbing); no unconditional
third-party calls; end-to-end tests on the real composition, not synthetic stand-ins; single code
path for any behavior that matters (the CLI/`propagate()` divergence class of bug).

---

## 2. REUSE / MODIFY / REMOVE / NEW BUILD

### REUSE (concepts and patterns, reimplemented in our codebase)

| Item | Where upstream | How we reuse |
|---|---|---|
| Multi-agent committee: 4 analysts → bull/bear debate → manager → trader → 3-persona risk debate → PM | `graph/setup.py` | Same topology, our prompts, output = `ResearchOpinion`, never a rating-to-execute |
| Deterministic debate-loop control (counters, complete path maps) | `conditional_logic.py`, `setup.py:32-42` | Same mechanics |
| Structured Pydantic outputs with enum vocabularies + bounds | `agents/schemas.py` | Same discipline; stricter — structured failure ⇒ opinion discarded, not free-text fallback |
| LLM provider factory, deep/quick split, Anthropic effort, retry budget | `llm_clients/` | Same design; Claude (`claude-fable-5` deep / cheaper quick tier) as primary |
| Verified-market-snapshot contract (deterministic numbers injected, LLM forbidden to invent) | `market_data_validator.py` | Reused as pattern on our intraday F&O snapshot, with freshness + cross-source checks added |
| Look-ahead guards: post-load date cut, half-open news windows, UTC normalization, stale-data typed errors | `stockstats_utils.py`, `yfinance_news.py` | Same patterns in our point-in-time store |
| Vendor registry with ordered fallback + typed error taxonomy | `dataflows/interface.py`, `errors.py` | Same pattern for our (Groww-primary) data providers |
| SQLite checkpointing for research workflows, shape-aware thread ids | `graph/checkpointer.py` | Same, research graph only — never for execution state |
| Post-trade reflection producing terse lessons + realized-outcome tagging | `graph/reflection.py`, `memory.py` | Reused, but keyed to our trade ledger, not yfinance returns |
| Env-override config with type coercion; path-traversal-safe cache keys | `default_config.py:10-68`, `utils.py:17-42` | Same patterns |
| Test culture: regression test per bug, vendor mocks, CI gate | `tests/` | Same culture from day one |

### MODIFY (keep the idea, change the contract)

| Item | Change |
|---|---|
| Final agent output | `PortfolioDecision`(rating) → **`ResearchOpinion`** (direction, conviction, horizon, evidence for/against, invalidation conditions, risks, data quality). Explicit "what would prove this wrong?" required. |
| Trader agent | Becomes Strategy Analyst: maps committee view onto *declared strategy templates*, no order fields |
| Conservative risk analyst | Prompt explicitly hunts hidden downside/liquidity/event risk/overconfidence/stale info/tail exposure; output feeds fusion as a veto-capable score, never as prose-only |
| Memory | Two stores (research memory / trading memory) in SQLite with typed schemas; retrieval by regime/setup similarity; may generate hypotheses only |
| Sentiment/news sources | Reddit-wallstreetbets/StockTwits/Fed queries → India-relevant sources; optional at first (empty ⇒ `data_quality` downgraded, never fabricated) |
| Checkpoint resume | Keep for research graph; fix the clobber-on-resume hazard by making report channels reducers or resume-aware |

### REMOVE (do not port)

| Item | Reason |
|---|---|
| BUY/SELL/HOLD & 5-tier rating as the terminal contract; regex `parse_rating` with silent `Hold` default | LLM ratings must never be an executable signal; silent defaults are the opposite of fail-closed |
| Free-text fallback after failed structured output | Schema failure must discard the opinion (fail closed), not degrade to prose |
| LLM free-text position sizing / entry / stop fields | Sizing is deterministic code in the risk engine, full stop |
| yfinance/Alpha Vantage as production market data; stockstats daily indicators | Wrong market, wrong granularity; replaced by Groww/NSE layer |
| US macro (FRED default), Fed/S&P news queries, wallstreetbets sentiment | Wrong market |
| Markdown-file memory as system of record | Replaced by SQLite event-sourced ledger + typed memories |
| Backtrader dependency | Listed in pyproject, never imported upstream; we build our own event-driven backtester with Indian costs |
| CLI/report-tree as primary interface | Replaced by service processes + dashboard; reporting concept kept |

### NEW BUILD (does not exist upstream in any form)

1. **India/NSE canonical market-data layer** — Groww REST + WebSocket adapters, instrument
   master, option chains, OI/IV/greeks, depth, IST session/holiday calendar, point-in-time
   storage, `VerifiedMarketSnapshot` with freshness attestation.
2. **Derivatives feature engine** — deterministic, timestamped: momentum/VWAP/ATR/RV, IV
   rank/percentile, term structure, skew, PCR, OI deltas, basis, breadth, opening range, spread &
   liquidity, greeks exposure.
3. **Regime engine** — deterministic classification incl. `EXPIRY_REGIME`, `EVENT_RISK`,
   `ILLIQUID`, `NO_TRADE`; strategies declare permitted regimes.
4. **Strategy framework** — versioned `Strategy` interface with declared entries/exits/stops/
   sizing rule/cost assumptions; registry; promotion state machine.
5. **Backtester** — event-driven, intraday, with full Indian cost stack (brokerage, STT, exchange
   charges, SEBI fees, stamp duty, GST, slippage/spread models), purged walk-forward CV, Monte
   Carlo, parameter-sensitivity tooling, survival gates + multi-objective scoring.
6. **SignalFusionEngine** — deterministic combination of quant signal, regime, and
   `ResearchOpinion` (enhance/reduce/veto per backtested rules) → `TradeIntent`.
7. **Deterministic RiskEngine** — the firewall (see `docs/RISK_POLICY.md`), kill switches,
   reconciliation gate, order-frequency limits, session/event calendars.
8. **Execution daemon** — isolated, idempotent (client reference ids +
   status-by-reference recovery on timeout), fill/partial/reject/cancel state machine, the only
   holder of production Groww credentials.
9. **BrokerAdapter hierarchy** — `SimulatedBroker`, `PaperBroker`, `GrowwReadOnlyAdapter`,
   `GrowwExecutionAdapter` behind one interface.
10. **Event-sourced portfolio/audit ledger** + reconciliation service (broker = truth).
11. **BACKTEST → PAPER → SHADOW → LIVE** environment pipeline with objective promotion/demotion
    and a manual arming mechanism for LIVE (restart always disarms).
12. **Observability** — dashboard (capital, P&L, drawdown, positions, signals, rejections with
    reason codes, feed freshness, latency, slippage, kill-switch state) + structured audit logs.
13. **Chaos/failure test suite** — timeout-after-submit, duplicate signal/order, partial fill,
    disconnects, stale data, restart-with-open-position, expiry day, halt.

---

## 3. Key risks the upstream design would have introduced (and our mitigations)

| Upstream hazard | Mitigation in target architecture |
|---|---|
| LLM rating flows straight to "execution" | LLM output is `ResearchOpinion` context; only deterministic strategies emit `TradeIntent`; only the risk engine approves |
| Silent `Hold` default on parse failure | Fail closed: unparseable/failed-schema opinion is discarded and logged; fusion proceeds without it (or vetoes if opinion was required) |
| Free-text fallback breaks schema guarantees | No free-text fallback anywhere in the decision path |
| `place_order == fill` assumption (would have been introduced by naive conversion) | Execution state machine: submitted → acked → partially_filled/filled/rejected/cancelled; broker reconciliation is the source of truth |
| Random client order ids (Groww SDK default) | Deterministic `order_reference_id` from intent hash; timeout ⇒ status-by-reference before any retry |
| Look-ahead via live fundamentals / filtered-empty news | Point-in-time store with publication timestamps; missing data ⇒ `data_quality` downgrade, never substitution |
| Host-local naive timestamps | tz-aware UTC storage + IST session calendar everywhere |
| Unbounded LLM loops (recursion_limit=100 only) | Research runs are budgeted (wall clock + token) and never on the order path |
