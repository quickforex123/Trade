"""Groww Trade API read-only market-data adapter.

Endpoint paths, parameters and response envelopes follow the official
growwapi==1.5.0 SDK, documented in docs/GROWW_API_REFERENCE.md. If the hosted
Groww documentation differs, the documentation wins — update the reference
doc and this adapter together.

This adapter holds a MARKET-DATA access token only. It exposes no order
methods and must never be constructed with order-capable credentials.
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import time as _time
import uuid
from datetime import UTC, date, datetime

import httpx

from qft.domain.enums import Exchange, OptionType, Segment
from qft.domain.instruments import Instrument
from qft.domain.market import Bar, FeedMeta, OptionChain, OptionQuote, Quote
from qft.domain.time import IST, ensure_utc

logger = logging.getLogger(__name__)

_BASE = "https://api.groww.in/v1"
_INSTRUMENTS_CSV = "https://growwapi-assets.groww.in/instruments/instrument.csv"

_CANDLE_INTERVALS = {
    "1m": "1minute",
    "5m": "5minute",
    "10m": "10minute",
    "15m": "15minute",
    "30m": "30minute",
    "1h": "1hour",
    "1d": "1day",
}


class GrowwDataError(Exception):
    pass


class GrowwRateLimited(GrowwDataError):
    pass


class _TokenBucket:
    """Client-side rate limiter kept below the documented API budget."""

    def __init__(self, rate_per_second: float, burst: int) -> None:
        self._rate = rate_per_second
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._last = _time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = _time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                _time.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


class GrowwReadOnlyAdapter:
    """Implements qft.data.provider.MarketDataProvider against Groww REST."""

    def __init__(
        self,
        access_token: str,
        client: httpx.Client | None = None,
        requests_per_second: float = 4.0,
    ) -> None:
        if not access_token:
            raise ValueError("access token required (market-data scope)")
        self._token = access_token
        self._client = client or httpx.Client(timeout=10.0)
        self._bucket = _TokenBucket(requests_per_second, burst=int(requests_per_second))
        self._instruments_cache: list[Instrument] | None = None
        self._instruments_cache_day: date | None = None

    # -- plumbing ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "x-request-id": str(uuid.uuid4()),
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "x-api-version": "1.0",
        }

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict:
        self._bucket.acquire()
        url = path if path.startswith("http") else _BASE + path
        try:
            resp = self._client.get(url, params=params, headers=self._headers())
        except httpx.TimeoutException as e:
            raise GrowwDataError(f"timeout GET {path}") from e
        if resp.status_code == 429:
            raise GrowwRateLimited(f"rate limited on {path}")
        if resp.status_code >= 400:
            # Never include the URL query (could carry identifiers) in errors.
            raise GrowwDataError(f"HTTP {resp.status_code} on {path}")
        body = resp.json()
        if isinstance(body, dict) and body.get("status") == "FAILURE":
            err = body.get("error") or {}
            raise GrowwDataError(f"API failure on {path}: {err.get('code')} {err.get('message')}")
        if isinstance(body, dict) and "payload" in body:
            payload = body["payload"]
            return payload if isinstance(payload, dict) else {"data": payload}
        return body if isinstance(body, dict) else {"data": body}

    @staticmethod
    def _meta(exchange_ts: datetime | None = None) -> FeedMeta:
        return FeedMeta(source="groww_rest", receive_ts=datetime.now(UTC), exchange_ts=exchange_ts)

    # -- instruments ------------------------------------------------------

    def instruments(self) -> list[Instrument]:
        today = datetime.now(IST).date()
        if self._instruments_cache is not None and self._instruments_cache_day == today:
            return self._instruments_cache
        self._bucket.acquire()
        resp = self._client.get(_INSTRUMENTS_CSV, timeout=60.0)
        if resp.status_code >= 400:
            raise GrowwDataError(f"instrument master fetch failed: HTTP {resp.status_code}")
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        if not rows:
            raise GrowwDataError("instrument master empty")
        required = {"exchange", "trading_symbol", "groww_symbol", "exchange_token"}
        missing = required - set(rows[0].keys())
        if missing:
            raise GrowwDataError(f"instrument master missing expected columns: {sorted(missing)}")
        out: list[Instrument] = []
        for r in rows:
            try:
                out.append(_instrument_from_master_row(r))
            except (ValueError, KeyError) as e:  # skip malformed rows, log once each
                logger.debug("skipping instrument row %s: %s", r.get("trading_symbol"), e)
        if not out:
            raise GrowwDataError("no parsable instruments in master")
        self._instruments_cache = out
        self._instruments_cache_day = today
        return out

    # -- quotes -----------------------------------------------------------

    def quote(self, instrument: Instrument) -> Quote:
        payload = self._get(
            "/live-data/quote",
            params={
                "exchange": instrument.exchange.value,
                "segment": instrument.segment.value,
                "trading_symbol": instrument.trading_symbol,
            },
        )
        return _quote_from_payload(instrument.key, payload, self._meta())

    def option_chain(self, underlying: str, expiry: date) -> OptionChain:
        payload = self._get(
            f"/option-chain/exchange/NSE/underlying/{underlying}",
            params={"expiry_date": expiry.isoformat()},
        )
        return _chain_from_payload(underlying, expiry, payload, self._meta())

    def expiries(self, underlying: str, year: int | None = None) -> list[date]:
        params: dict[str, str] = {"exchange": "NSE", "underlying_symbol": underlying}
        if year is not None:
            params["year"] = str(year)
        payload = self._get("/historical/expiries", params=params)
        raw = payload.get("expiries") or payload.get("data") or []
        out: list[date] = []
        for item in raw:
            value = item.get("expiry") if isinstance(item, dict) else item
            try:
                out.append(date.fromisoformat(str(value)[:10]))
            except ValueError:
                logger.warning("unparsable expiry value %r for %s", value, underlying)
        return sorted(out)

    def historical_bars(
        self, instrument: Instrument, start: datetime, end: datetime, interval: str
    ) -> list[Bar]:
        if interval not in _CANDLE_INTERVALS:
            raise ValueError(f"unsupported interval {interval!r}")
        start = ensure_utc(start)
        end = ensure_utc(end)
        payload = self._get(
            "/historical/candles",
            params={
                "exchange": instrument.exchange.value,
                "segment": instrument.segment.value,
                "groww_symbol": instrument.groww_symbol or instrument.trading_symbol,
                "start_time": start.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": end.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
                "candle_interval": _CANDLE_INTERVALS[interval],
            },
        )
        candles = payload.get("candles") or payload.get("data") or []
        bars: list[Bar] = []
        for c in candles:
            bar = _bar_from_candle(instrument.key, interval, c)
            if bar is not None:
                bars.append(bar)
        return bars


# -- payload mappers (module-level for testability) -------------------------


def _instrument_from_master_row(r: dict[str, str]) -> Instrument:
    def _f(key: str) -> str:
        return (r.get(key) or "").strip()

    exchange = Exchange(_f("exchange")) if _f("exchange") in Exchange.__members__ else None
    if exchange is None:
        raise ValueError(f"unsupported exchange {_f('exchange')!r}")
    segment_raw = _f("segment") or ("FNO" if _f("instrument_type") in {"FUT", "CE", "PE"} else "CASH")
    segment = Segment(segment_raw) if segment_raw in Segment.__members__ else Segment.CASH
    itype = _f("instrument_type")
    option_type = OptionType(itype) if itype in ("CE", "PE") else None
    expiry_raw = _f("expiry_date") or _f("expiry")
    expiry = date.fromisoformat(expiry_raw[:10]) if expiry_raw else None
    strike_raw = _f("strike_price") or _f("strike")
    strike = float(strike_raw) if strike_raw else None
    if option_type is not None and strike is not None and strike > 100_000_000:
        # some masters publish strike in paise
        strike = strike / 100.0
    lot_raw = _f("lot_size")
    tick_raw = _f("tick_size")
    return Instrument(
        exchange=exchange,
        segment=segment,
        trading_symbol=_f("trading_symbol"),
        groww_symbol=_f("groww_symbol"),
        exchange_token=_f("exchange_token"),
        underlying=_f("underlying_symbol") or _f("underlying"),
        instrument_type=itype,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        lot_size=int(float(lot_raw)) if lot_raw else 1,
        tick_size=float(tick_raw) if tick_raw else 0.05,
        freeze_quantity=int(float(_f("freeze_quantity"))) if _f("freeze_quantity") else None,
    )


def _quote_from_payload(instrument_key: str, p: dict, meta: FeedMeta) -> Quote:
    ltp = p.get("last_price") or p.get("ltp") or 0.0
    depth = p.get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    bid = float(buys[0]["price"]) if buys and buys[0].get("price") else p.get("bid_price")
    ask = float(sells[0]["price"]) if sells and sells[0].get("price") else p.get("offer_price")
    return Quote(
        instrument_key=instrument_key,
        meta=meta,
        ltp=float(ltp),
        bid=float(bid) if bid else None,
        ask=float(ask) if ask else None,
        bid_qty=int(buys[0]["quantity"]) if buys and buys[0].get("quantity") else None,
        ask_qty=int(sells[0]["quantity"]) if sells and sells[0].get("quantity") else None,
        volume=float(p["volume"]) if p.get("volume") else None,
        open_interest=float(p["open_interest"]) if p.get("open_interest") else None,
    )


def _chain_from_payload(underlying: str, expiry: date, p: dict, meta: FeedMeta) -> OptionChain:
    underlying_price = float(
        p.get("underlying_price") or p.get("spot_price") or p.get("underlying_ltp") or 0.0
    )
    rows: list[OptionQuote] = []
    raw_rows = p.get("option_chain") or p.get("chain") or p.get("data") or []
    if isinstance(raw_rows, dict):
        raw_rows = list(raw_rows.values())
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        strike_raw = item.get("strike_price") or item.get("strike") or 0
        strike = float(strike_raw)
        if strike > 100_000_000:
            strike = strike / 100.0
        for leg_key, ot in (("call_option", OptionType.CE), ("put_option", OptionType.PE)):
            leg = item.get(leg_key)
            if not isinstance(leg, dict) or not leg.get("trading_symbol"):
                continue
            try:
                inst = Instrument(
                    exchange=Exchange.NSE,
                    segment=Segment.FNO,
                    trading_symbol=str(leg["trading_symbol"]),
                    groww_symbol=str(leg.get("groww_symbol") or ""),
                    underlying=underlying,
                    instrument_type=ot.value,
                    expiry=expiry,
                    strike=strike,
                    option_type=ot,
                    lot_size=int(leg.get("lot_size") or 1),
                )
                quote = Quote(
                    instrument_key=inst.key,
                    meta=meta,
                    ltp=float(leg.get("ltp") or leg.get("last_price") or 0.0),
                    bid=float(leg["bid_price"]) if leg.get("bid_price") else None,
                    ask=float(leg["ask_price"]) if leg.get("ask_price") else None,
                    volume=float(leg["volume"]) if leg.get("volume") else None,
                    open_interest=float(leg["open_interest"]) if leg.get("open_interest") else None,
                )
                greeks = leg.get("greeks") or leg
                rows.append(
                    OptionQuote(
                        instrument=inst,
                        quote=quote,
                        iv=_maybe_float(greeks.get("iv") or greeks.get("implied_volatility")),
                        delta=_maybe_float(greeks.get("delta")),
                        gamma=_maybe_float(greeks.get("gamma")),
                        theta=_maybe_float(greeks.get("theta")),
                        vega=_maybe_float(greeks.get("vega")),
                        oi=_maybe_float(leg.get("open_interest")),
                        oi_change=_maybe_float(
                            leg.get("open_interest_change") or leg.get("oi_change")
                        ),
                    )
                )
            except (ValueError, TypeError) as e:
                logger.debug("skipping chain leg %s: %s", leg.get("trading_symbol"), e)
    return OptionChain(
        underlying=underlying,
        expiry=expiry.isoformat(),
        meta=meta,
        underlying_price=underlying_price,
        rows=tuple(rows),
    )


def _bar_from_candle(instrument_key: str, interval: str, c: object) -> Bar | None:
    """Candle arrives as [epoch_or_iso, o, h, l, c, volume, (oi)] or a dict."""
    try:
        if isinstance(c, dict):
            ts_raw = c.get("time") or c.get("timestamp") or c.get("start_time")
            values = [c.get("open"), c.get("high"), c.get("low"), c.get("close"), c.get("volume")]
            oi = c.get("open_interest")
        elif isinstance(c, (list, tuple)) and len(c) >= 5:
            ts_raw, *values = c[:6]
            values = list(values) + [0] * (5 - len(values))
            oi = c[6] if len(c) > 6 else None
        else:
            return None
        ts = _parse_candle_ts(ts_raw)
        o, h, low, close = (float(values[0]), float(values[1]), float(values[2]), float(values[3]))
        vol = float(values[4] or 0)
        return Bar(
            instrument_key=instrument_key,
            ts=ts,
            interval=interval,
            open=o,
            high=h,
            low=low,
            close=close,
            volume=vol,
            open_interest=_maybe_float(oi),
        )
    except (ValueError, TypeError, IndexError) as e:
        logger.warning("unparsable candle for %s: %s", instrument_key, e)
        return None


def _parse_candle_ts(ts_raw: object) -> datetime:
    if isinstance(ts_raw, (int, float)):
        # epoch seconds (Groww uses seconds; guard ms)
        seconds = float(ts_raw) / (1000.0 if float(ts_raw) > 1e12 else 1.0)
        return datetime.fromtimestamp(seconds, tz=UTC)
    text = str(ts_raw)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as e:
        raise ValueError(f"unparsable candle timestamp {text!r}") from e
    if parsed.tzinfo is None:
        # Groww returns naive IST wall-clock times
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(UTC)


def _maybe_float(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
