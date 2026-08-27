# QFT — Quantitative F&O Trading Platform (NSE / Groww)

A personal, institutional-grade systematic trading research and execution platform for Indian
index derivatives (initially NIFTY F&O), built around one objective:

> **Maximize long-term, after-cost, risk-adjusted return while minimizing probability of ruin,
> drawdown, overfitting and execution failure.**

Doing **nothing** when no statistically attractive opportunity exists is a first-class, positive
outcome of this system.

## Architecture in one line

**LLMs propose. Deterministic code authorizes. The broker adapter executes.**

```
market data → validation → features/regime ─┬→ quant strategies ─┐
                                            └→ research committee ┴→ signal fusion → TradeIntent
                → deterministic RISK FIREWALL (approve/reject) → execution daemon → Groww → NSE
```

- The AI research layer (adapted from TradingAgents, running on Claude) produces
  `ResearchOpinion` objects — context, never orders.
- The **RiskEngine** is deterministic, configuration-driven, and cannot be modified or bypassed by
  any LLM output.
- The **execution daemon** is the only component that may hold the production Groww secret, and
  live trading is disabled by default behind an explicit manual arming mechanism.

## Key documents

| Document | Purpose |
|---|---|
| [`ARCHITECTURE_AUDIT.md`](ARCHITECTURE_AUDIT.md) | Audit of the upstream TradingAgents repo: what is reused, modified, removed |
| [`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md) | The platform design: modules, data flow, environments, promotion pipeline |
| [`docs/GROWW_API_REFERENCE.md`](docs/GROWW_API_REFERENCE.md) | Verified Groww Trade API surface (from official SDK v1.5.0) + SEBI/NSE constraints |
| [`docs/RISK_POLICY.md`](docs/RISK_POLICY.md) | The deterministic risk firewall rules and configured limits |

## Environments

`BACKTEST → PAPER → SHADOW → LIVE` — strategies are promoted only by objective criteria and
demoted automatically. LIVE is off by default; a restart can never re-arm it.

## Safety invariants (non-negotiable)

1. No LLM ever places, sizes, or authorizes an order.
2. The production Groww API secret exists only in the isolated execution daemon's runtime
   environment — never in this repo, never in research/backtest/dashboard code.
3. Stale or unverifiable market data ⇒ no trade (fail closed).
4. Broker positions are the source of truth; unexplained mismatch ⇒ halt new trading.
5. If the minimum lot size violates the risk budget ⇒ do not trade.
6. Every order is idempotent, explainable, and reproducible from the audit ledger.
