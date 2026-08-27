"""Point-in-time bar store.

Append-only parquet files per (instrument, interval), each row stamped with
`captured_at`. The read API takes a mandatory `as_of` and physically cannot
serve rows whose bar CLOSE time is after it — look-ahead safety lives in the
storage interface, not in caller discipline.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from qft.domain.market import Bar
from qft.domain.time import ensure_utc, now_utc

_INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "1d": 86400,
}

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")


class PITBarStore:
    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, instrument_key: str, interval: str) -> Path:
        safe = instrument_key.replace(":", "_")
        if not _SAFE_COMPONENT.match(safe) or ".." in safe:
            raise ValueError(f"unsafe instrument key {instrument_key!r}")
        if interval not in _INTERVAL_SECONDS:
            raise ValueError(f"unsupported interval {interval!r}")
        return self._root / f"{safe}__{interval}.parquet"

    def append(self, bars: list[Bar]) -> int:
        """Append bars (idempotent on (instrument, interval, ts)). Returns rows added."""
        if not bars:
            return 0
        captured = now_utc()
        added = 0
        by_file: dict[Path, list[Bar]] = {}
        for b in bars:
            by_file.setdefault(self._path(b.instrument_key, b.interval), []).append(b)
        for path, group in by_file.items():
            new = pd.DataFrame(
                {
                    "instrument_key": [b.instrument_key for b in group],
                    "ts": [b.ts for b in group],
                    "interval": [b.interval for b in group],
                    "open": [b.open for b in group],
                    "high": [b.high for b in group],
                    "low": [b.low for b in group],
                    "close": [b.close for b in group],
                    "volume": [b.volume for b in group],
                    "open_interest": [b.open_interest for b in group],
                    "captured_at": captured,
                }
            )
            if path.exists():
                old = pd.read_parquet(path)
                before = len(old)
                merged = pd.concat([old, new], ignore_index=True)
                merged = merged.drop_duplicates(subset=["instrument_key", "ts"], keep="first")
                added += len(merged) - before
                merged = merged.sort_values("ts")
                merged.to_parquet(path, index=False)
            else:
                new = new.drop_duplicates(subset=["instrument_key", "ts"], keep="first")
                new.sort_values("ts").to_parquet(path, index=False)
                added += len(new)
        return added

    def bars(
        self,
        instrument_key: str,
        interval: str,
        start: datetime,
        end: datetime,
        as_of: datetime,
    ) -> list[Bar]:
        """Bars in [start, end) whose bar-close <= as_of. The as_of cut is
        unconditional — a caller cannot opt out of look-ahead protection."""
        start, end, as_of = ensure_utc(start), ensure_utc(end), ensure_utc(as_of)
        path = self._path(instrument_key, interval)
        if not path.exists():
            return []
        df = pd.read_parquet(path)
        if df.empty:
            return []
        bar_span = timedelta(seconds=_INTERVAL_SECONDS[interval])
        ts = pd.to_datetime(df["ts"], utc=True)
        mask = (ts >= start) & (ts < end) & ((ts + bar_span) <= as_of)
        out: list[Bar] = []
        for _, row in df.loc[mask].sort_values("ts").iterrows():
            oi = row["open_interest"]
            out.append(
                Bar(
                    instrument_key=row["instrument_key"],
                    ts=pd.Timestamp(row["ts"]).to_pydatetime(),
                    interval=row["interval"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    open_interest=None if pd.isna(oi) else float(oi),
                )
            )
        return out
