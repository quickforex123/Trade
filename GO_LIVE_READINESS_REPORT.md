# GO-LIVE READINESS REPORT

**Status: NOT READY FOR LIVE TRADING. Live execution remains disabled by default.**

This report is the honest gate between the software being *finished* and real
capital being *at risk*. Per the platform charter, completing the software is
explicitly not a reason to go live. This document must be re-issued with every
section green — on real data and real paper/shadow runs — before the operator
may even consider arming LIVE.

Date: 2026-08-27 · Environment: development · Capital at risk today: ₹0

---

## 1. What is DONE and verified (by 88 automated tests, CI-gated)

| Area | Status | Evidence |
|---|---|---|
| Typed domain contracts (frozen models, UTC-only, legal order-state machine) | ✅ | `tests/test_domain.py` |
| Indian F&O cost model to the paisa (brokerage/STT/exchange/SEBI/stamp/GST) | ✅ | `tests/test_costs.py` |
| NSE IST session calendar + holiday handling | ✅ | `tests/test_calendar_validation.py` |
| Fail-closed snapshot verification (stale/future/implausible/closed ⇒ unverified) | ✅ | `tests/test_calendar_validation.py` |
| Point-in-time store with unconditional as-of cut (look-ahead impossible via API) | ✅ | `tests/test_pit_features_regime.py` |
| Deterministic feature/regime engines (leak-rejecting, hysteresis) | ✅ | `tests/test_pit_features_regime.py` |
| Risk firewall: 12-gate validation, all reason codes, property-based invariant "no approval exceeds loss headroom" | ✅ | `tests/test_risk_engine.py` |
| Kill switches (SOFT/HARD, restart keeps most restrictive, corrupt file ⇒ HARD) | ✅ | `tests/test_risk_engine.py` |
| LIVE arming (exact phrase, expiry, restart always disarms) | ✅ | `tests/test_risk_engine.py` |
| Execution daemon idempotency: timeout-before/after-book, duplicate submits, restart adoption, partial fills — no duplicate orders in any scenario | ✅ | `tests/test_execution.py` (chaos) |
| Event-sourced ledger; broker-truth reconciliation trips SOFT kill on mismatch | ✅ | `tests/test_execution.py` |
| Backtester honesty (next-bar-open fills, stop-first conservatism, min-lot rule, square-off) | ✅ | `tests/test_backtest.py` |
| Survival gates + multi-objective scoring + walk-forward with strict temporal separation | ✅ | `tests/test_backtest.py` |
| Fusion: bounded AI influence (boost/dampen/veto), defined-risk long premium only | ✅ | `tests/test_fusion_research.py` |
| Research committee fail-closed (schema failure ⇒ no opinion, no fallback) | ✅ | `tests/test_fusion_research.py` |
| Full PAPER pipeline end-to-end; SHADOW provably cannot submit orders | ✅ | `tests/test_loop_dashboard.py` |
| Production Groww adapter unconstructible outside `QFT_ENVIRONMENT=LIVE` | ✅ | `tests/test_brokers_extra.py` |
| CI: tests + ruff + mypy(strict core) + architecture-boundary + secret scan | ✅ | `.github/workflows/ci.yml` |

## 2. What is NOT done — every item blocks LIVE

| # | Blocker | Why it blocks |
|---|---|---|
| 1 | **No real historical data ingested.** The PIT store is empty; every backtest so far ran on synthetic bars that verify *mechanics only*. | No strategy has any evidence of edge. Zero strategies are production-eligible. |
| 2 | **No strategy has passed Stage-A survival gates on real data**, let alone walk-forward Stage-B scoring. | The charter forbids promotion without out-of-sample evidence. |
| 3 | **Groww live feed (WebSocket/NATS) adapter not yet integrated** — REST polling is implemented; the streaming client and its reconnect/stale-detection drills are not. | Intraday decisions on polled quotes alone have unverified latency characteristics. |
| 4 | **Groww response-schema fields verified against SDK v1.5.0, not against live responses.** Field-name fallbacks in the adapters must be confirmed against the real API during SHADOW. | Guessed field names are forbidden; SHADOW exists to verify them with zero order risk. |
| 5 | **No PAPER run history.** Requirement: ≥ 20 trading days of paper trading with tracking error vs. backtest expectation within bounds. | Untested live-data behaviour. |
| 6 | **No SHADOW run history.** Requirement: ≥ 10 trading days on production architecture with real broker data and zero orders. | Final verification layer before capital. |
| 7 | **Margin API integration not exercised** (`/margins/detail/orders`) — the risk engine uses conservative premium-based gating; broker-quoted margin must be wired for any non-long-premium structure. | Fail-closed today (long premium only), but must be done before any spread structures. |
| 8 | **SEBI/broker operational prerequisites**: static IP registration with Groww, API key provisioning under the 2025 framework, verification of current rate limits from the official docs page. | Regulatory compliance. |
| 9 | **Event blackout calendar is empty** — RBI policy dates, budget, expiry-day special handling must be populated for the trading year. | EVENT_RISK gate currently has nothing to gate on. |
| 10 | **Chaos drills against the real venue in SHADOW** (WS disconnect, restart with open position, expiry-day behaviour) not performed. | Simulated chaos only proves the logic, not the venue behaviour. |

## 3. Promotion criteria (unchanged from TARGET_ARCHITECTURE.md §12)

A strategy may trade LIVE only when ALL hold:

1. Stage-A survival gates pass on ≥ 2 years (or maximum available) of real intraday data.
2. Stage-B score ≥ 0.45 with fold consistency ≥ 0.6.
3. ≥ 20 PAPER days: realized expectancy within 1σ of backtest expectation; slippage within model.
4. ≥ 10 SHADOW days: zero unexplained rejections, zero reconciliation mismatches, feed freshness ≥ 99% in-session.
5. Operator review of this report re-issued with sections 2 items 1–10 all resolved.
6. Manual arming (`qft arm-live`) — which still expires and disarms on restart.

## 4. Standing risk acknowledgements

- With ₹50,000 capital and current NIFTY option premiums, most ATM structures
  exceed the 1.5% per-trade risk budget; the firewall will reject them and the
  correct behaviour is **no trade**. Expect low trade frequency by design.
- LLM research (Claude) can only boost/dampen/veto within backtested bands; it
  cannot create, size, or authorize an order. This must remain true in code
  review of every future change (CI boundary check enforces import isolation).
- Synthetic-data results are never evidence. Any report or claim of strategy
  profitability must cite real-data walk-forward artifacts stored in the repo.

**Conclusion: build phase complete; evidence phase not started. DO NOT ARM LIVE.**
