# Groww Trade API — Verified Reference (for this platform)

**Source of truth:** the official `growwapi` Python SDK, version **1.5.0** (released 2025-12-06,
PyPI), introspected directly from the published wheel — not guessed. Where the hosted docs at
`https://groww.in/trade-api/docs` differ from this file, **the current official docs win**; update
this file and the adapters together. Do not code against endpoints that are not listed here or in
the official docs.

Pinned SDK: `growwapi==1.5.0` (Python `>=3.9,<4.0`).

## Authentication

Two-step:

1. **API key + (TOTP | approval secret) → access token**
   `POST https://api.groww.in/v1/token/api/access`
   - TOTP flow: body `{"key_type": "totp", "totp": "<code>"}`
   - Approval flow: body `{"key_type": "approval", "checksum": sha256(secret + unix_seconds), "timestamp": unix_seconds}`
   - `Authorization: Bearer <api_key>` header; response contains `token`.
   - SDK helper: `GrowwAPI.get_access_token(api_key, totp=... | secret=...)` (exactly one of the two).
2. **All API calls** use `Authorization: Bearer <access_token>` plus headers:
   `x-request-id` (uuid4 per request), `x-api-version: 1.0`, `Content-Type: application/json`.

> Platform rule: the API key and approval secret are **execution-daemon-only** secrets.
> Read-only market-data usage still requires a token; the read-only adapter must be given a token
> with the narrowest scope Groww offers, injected via environment/secret manager at runtime.

## Base URL

`https://api.groww.in/v1`

## REST endpoints (from SDK v1.5.0)

### Live data (read-only)
| Purpose | Method + path | Params |
|---|---|---|
| Quote (full) | `GET /live-data/quote` | `exchange`, `segment`, `trading_symbol` |
| LTP (multi) | `GET /live-data/ltp` | `segment`, `exchange_symbols` (e.g. `NSE_NIFTY25JAN25000CE`, comma/tuple) |
| OHLC (multi) | `GET /live-data/ohlc` | `segment`, `exchange_symbols` |
| Option Greeks | `GET /live-data/greeks/exchange/{exchange}/underlying/{underlying}/trading_symbol/{trading_symbol}/expiry/{yyyy-MM-dd}` | path params |
| Option chain (with Greeks, all strikes) | `GET /option-chain/exchange/{exchange}/underlying/{underlying}` | `expiry_date=YYYY-MM-DD` |

### Historical data (read-only)
| Purpose | Method + path | Params |
|---|---|---|
| Candles (V2, current) | `GET /historical/candles` | `exchange`, `segment`, `groww_symbol`, `start_time`, `end_time` (`yyyy-MM-dd HH:mm:ss`), `candle_interval` (`1minute`,`2minute`,`3minute`,`5minute`,`10minute`,`15minute`,`30minute`,`1hour`,`4hour`,`1day`,`1week`,`1month`) |
| Candles (V1, **deprecated**) | `GET /historical/candle/range` | `interval_in_minutes` — do not use in new code |
| Expiry dates | `GET /historical/expiries` | `exchange`, `underlying_symbol`, optional `year`, `month` |
| Contracts for expiry | `GET /historical/contracts` | `exchange`, `underlying_symbol`, `expiry_date` |

### Instruments (read-only)
- Master CSV: `https://growwapi-assets.groww.in/instruments/instrument.csv` (string-typed columns;
  includes at least `exchange`, `trading_symbol`, `groww_symbol`, `exchange_token`; treat the CSV
  header as authoritative at runtime — refresh daily, cache locally, and validate expected columns
  before use).
- SDK lookups: by `exchange + trading_symbol`, by `groww_symbol`, by `exchange_token`.

### Account / portfolio (read-only)
| Purpose | Method + path |
|---|---|
| Holdings | `GET /holdings/user` |
| Positions (all) | `GET /positions/user` (`segment` optional) |
| Position for symbol | `GET /positions/trading-symbol` (`trading_symbol`, `segment`) |
| Available margin | `GET /margins/detail/user` |
| Required margin for order(s) | `POST /margins/detail/orders?segment=` body: list of `{trading_symbol, transaction_type, quantity, price, order_type, product, exchange}` |
| User profile | `GET /user/detail` |

### Orders (EXECUTION DAEMON ONLY)
| Purpose | Method + path | Notes |
|---|---|---|
| Place | `POST /order/create` | body: `trading_symbol, quantity, price, trigger_price, validity, exchange, segment, product, order_type, transaction_type, order_reference_id` |
| Modify | `POST /order/modify` | by `groww_order_id` |
| Cancel | `POST /order/cancel` | by `groww_order_id` + `segment` |
| Detail | `GET /order/detail/{groww_order_id}?segment=` | |
| Status | `GET /order/status/{groww_order_id}?segment=` | |
| **Status by client reference** | `GET /order/status/reference/{order_reference_id}?segment=` | **idempotency backbone** — resolve timeouts by querying our own reference id |
| List | `GET /order/list` | paginated |
| Trades for order | `GET /order/trades/{groww_order_id}?segment=&page=&page_size=` | fills/partial fills |
| Smart orders (GTT/OCO) | `POST /order-advance/create`, `PUT /order-advance/modify/{id}`, `POST /order-advance/cancel/{segment}/{type}/{id}`, `GET /order-advance/status/...`, `GET /order-advance/list` | OCO = server-side target+stop pair |

**Idempotency facts (verified from SDK):**
- `order_reference_id` is client-supplied; SDK defaults to a *random 8-digit number* — we must
  ALWAYS supply our own deterministic reference id (never rely on the SDK default).
- After a network timeout on `place_order`, the daemon must call
  `get_order_status_by_reference(segment, order_reference_id)` before any retry.

### Enumerations (verified constants)
- Validity: `DAY`, `EOS`, `IOC`, `GTC`, `GTD`
- Exchange: `NSE`, `BSE`, `MCX`, `MCXSX`, `NCDEX`, `US`
- Order type: `LIMIT`, `MARKET`, `SL`, `SL_M`
- Product: `CNC`, `MIS`, `NRML`, `MTF`, `BO`, `CO`, `ARB`
- Segment: `CASH`, `FNO`, `CURRENCY`, `COMMODITY`
- Transaction: `BUY`, `SELL`
- Smart order: `GTT`, `OCO`; status `ACTIVE/TRIGGERED/CANCELLED/EXPIRED/FAILED/COMPLETED`; trigger direction `UP/DOWN`

### Error model
Response envelope `{status, payload | error{code,message}}`. HTTP → exception map:
400 BadRequest, 401 Authentication, 403 Authorisation, 404 NotFound, **429 RateLimit**, 504 Timeout.
The adapter must treat 429 with backoff and surface a typed error; the risk engine treats repeated
429/5xx as degraded connectivity (fail closed).

## Live streaming feed (WebSocket/NATS)

- URL: `wss://socket-api.groww.in` — NATS protocol; auth via socket JWT obtained from
  `POST https://api.groww.in/v1/api/apex/v1/socket/token/create/` with an nkeys public key
  (SDK: `GrowwFeed`, generates ed25519 keypair per session).
- Payloads are **protobuf**, parsed by the SDK to dicts.
- Topics (subscription key = `exchange_token` from the instruments CSV):
  - FNO NSE prices: `/ld/fo/nse/price.{token}`; detailed: `/ld/fo/nse/price_detailed.{token}`
  - FNO NSE depth: `/ld/fo/nse/book.{token}`
  - Equity NSE: `/ld/eq/nse/price.{token}`, `.../price_detailed.`, `.../book.`
  - Index values: `/ld/indices/nse/price.{token}`
  - Own order updates: `stocks_fo/order/updates.apex.{subscriptionId}` (FNO), `stocks/order/updates.apex.{subscriptionId}` (equity)
  - Own position updates: `stocks_fo/position/updates.apex.{subscriptionId}`
- SDK methods: `subscribe_ltp`, `subscribe_market_depth`, `subscribe_index_value`,
  `subscribe_fno_order_updates`, `subscribe_fno_position_updates` (+ matching `get_*`/`unsubscribe_*`).
- Order-update normalization in SDK: `buySell B/S → transactionType BUY/SELL`, `MKT/L → MARKET/LIMIT`.

## Rate limits

Documented publicly (docs site) as per-category budgets (orders / live data / non-trading);
the numbers must be confirmed from the current official docs page before live use and encoded in
`config` — the adapter enforces a client-side token bucket below the documented ceiling.
429 responses are authoritative regardless of configured limits.

## SEBI retail algo framework (operative constraints)

Per SEBI circular SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 (Feb 2025; fully mandatory
2026-04-01):

- API access requires **client-specific API key + broker-whitelisted static IP**. The production
  daemon must run from the registered static IP.
- **≤ 10 orders per second** per exchange without algo registration; above that the algo must be
  registered via the broker. Platform hard cap: our order-frequency limiter must keep us far below
  this (default ≤ 1 order/sec, configurable only downward of the regulatory bound).
- Broker is principal; algo providers act as agents. A personal self-built system trading one's own
  account still flows through the broker's API controls (static IP + key). Respect all broker-side
  throttles; never attempt to evade OPS categorization.

## NSE contract facts (verify at runtime, never hardcode)

- NIFTY index derivatives lot size: **65 from the Jan-2026 series** (was 75 during 2025, 25 before
  Apr-2025). Lot sizes change by exchange circular ⇒ always read lot size from the instruments
  master / `get_contracts`, never from constants.
- Weekly index option expiry day has changed historically (Thursday → Tuesday in 2025) ⇒ always
  read expiries from `GET /historical/expiries`, never compute "next Thursday".
- Index options are European, cash-settled; STT/CTT, exchange transaction charges, SEBI fees, GST,
  stamp duty apply — cost model maintained in `qft/costs/` with rates in config, reviewed against
  current schedules.
