# Risk Policy — Deterministic Risk Firewall

This document defines the risk rules enforced by `qft/risk/engine.py`. The engine is pure,
deterministic, configuration-driven code. **No LLM output can create, modify, or bypass any rule
here.** Config changes require a human-edited file and process restart; they are logged to the
audit ledger.

## Fail-closed doctrine

Any of the following ⇒ the affected trade intent is REJECTED (and where noted, all new trading
halts):

- market data unverifiable or stale
- broker connectivity degraded
- broker vs. internal position mismatch (⇒ HALT new trading until reconciled)
- unexplained margin change (⇒ HALT)
- market closed / not in allowed session window
- kill switch active (soft or hard)
- risk config failed validation at startup (⇒ process refuses to start)

## Baseline limits for INR 50,000 capital (initial, conservative)

All values live in `config/risk.yaml` and are validated by a Pydantic schema at startup. These are
the defaults; they may be tightened at will, but loosening requires deliberate manual edit.

| Rule | Default | Rationale |
|---|---|---|
| `max_capital_at_risk_per_trade` | 1.5% of equity (₹750) | classic fixed-fractional, survival-first |
| `max_premium_per_trade` | ₹6,000 | caps long-option outlay |
| `max_daily_loss` | 3% of equity (₹1,500) | two max-loss trades stop the day |
| `max_weekly_loss` | 6% of equity | circuit breaker above daily |
| `max_drawdown_halt` | 10% from equity high-water mark | HARD halt, manual restart required |
| `max_concurrent_positions` | 1 | tiny book; no correlation math needed yet |
| `max_lots_per_order` | 1 | minimum size only |
| `max_orders_per_day` | 6 | overtrading guard |
| `max_orders_per_minute` | 2 | fat-loop guard; far below SEBI 10 orders/sec threshold |
| `max_strategy_exposure` | 100% of one position | single strategy at a time initially |
| `instrument_allowlist` | NIFTY index options + futures only | liquidity |
| `expiry_allowlist` | nearest two weekly + current monthly | avoids illiquid far series |
| `max_spread_pct` | 0.75% of mid (options), 0.05% (futures) | executability |
| `min_open_interest` | 1,500,000 (index options) | liquidity floor |
| `max_slippage_budget` | strategy-declared, capped at 1.5% of premium | |
| `signal_ttl_seconds` | 20 | stale intent ⇒ reject |
| `max_feed_age_seconds` | 3 (ticks), 90 (snapshot) | stale data ⇒ reject |
| `session_window` | 09:20–15:00 IST entries; square-off by 15:10 | avoids open/close auctions and MIS force-close |
| `event_risk_blackout` | RBI policy days, budget day, election result days (config list) | no entries |
| `naked_short_options` | **forbidden** (structural) | defined-risk only |
| `martingale/averaging down` | **forbidden** (structural) | no size increase after losses |

**Lot-size rule:** if `estimated_max_loss(1 lot) > max_capital_at_risk_per_trade`, the trade is
rejected. The engine never shrinks the stop or reinterprets risk to make minimum size fit.

## Validation sequence (every TradeIntent)

1. Kill-switch state (hard ⇒ reject; soft ⇒ reject new entries, allow exits)
2. Environment gate (LIVE requires explicit armed state; restart always disarms)
3. Signal integrity: schema, signature, expiry, duplicate intent hash
4. Market state: session window, holiday calendar, event blackout
5. Data integrity: verified snapshot present, fresh, instrument matches
6. Instrument gates: allowlist, expiry allowlist, lot size validity vs. contract master
7. Liquidity gates: spread, depth, open interest
8. Capital gates: available cash, margin required (broker-quoted) vs. available, premium cap
9. Loss gates: per-trade max loss, daily/weekly loss remaining, drawdown state
10. Exposure gates: concurrent positions, per-strategy, per-underlying
11. Frequency gates: orders/minute, orders/day, duplicate order in flight
12. Reconciliation gate: broker positions == internal ledger (else HALT)

Every decision produces a `RiskDecision{approved, reason_codes[], evaluated_rules[], snapshot_ids}`
persisted to the ledger — including approvals.

## Kill switches

- `SOFT_KILL`: no new entries; exits/square-off allowed. Auto-triggers: daily loss hit, feed
  degraded, reconciliation mismatch.
- `HARD_KILL`: no orders of any kind except operator-confirmed FLATTEN_ALL. Auto-triggers:
  drawdown halt, repeated broker errors, position mismatch that worsens.
- `FLATTEN_ALL`: market-order square-off of all positions, operator-initiated (or auto at
  session-end square-off time for intraday products).
- `DISABLE_STRATEGY(id)`: per-strategy circuit breaker — auto-triggers on strategy daily loss,
  N consecutive losses beyond historical expectation, or live slippage grossly exceeding model.
- Kill-switch state is persisted; a restart preserves the most restrictive state.

## What memory/research may NOT do

Research memory and reflection may generate hypotheses and proposals only. Production limit
changes require: fresh backtest + walk-forward evidence, human review, manual config edit,
restart. There is no code path from any LLM or memory store to this file or to `config/risk.yaml`.
