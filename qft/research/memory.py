"""Research and trading memories.

RESEARCH MEMORY: past committee opinions + realized outcomes.
TRADING MEMORY: quantitative record of every completed trade.

Retrieval is deterministic (regime/strategy/setup keyed, recency-bounded).
Memory may generate hypotheses and prompt context ONLY — there is no code
path from here to strategies, fusion rules, or risk configuration.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from qft.domain.portfolio import TradeRecord
from qft.domain.research import ResearchOpinion
from qft.domain.time import ensure_utc, now_utc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_memory (
    opinion_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    underlying TEXT NOT NULL,
    regime TEXT NOT NULL,
    direction TEXT NOT NULL,
    conviction REAL NOT NULL,
    payload TEXT NOT NULL,
    outcome_pnl REAL,
    outcome_note TEXT,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS trading_memory (
    trade_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    regime TEXT NOT NULL,
    setup_digest TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rm_underlying ON research_memory(underlying, ts);
CREATE INDEX IF NOT EXISTS idx_tm_strategy ON trading_memory(strategy_id, regime, ts);
"""


class MemoryStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- research memory -----------------------------------------------------

    def store_opinion(self, opinion: ResearchOpinion) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO research_memory "
                "(opinion_id, ts, underlying, regime, direction, conviction, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    opinion.opinion_id,
                    opinion.ts.isoformat(),
                    opinion.instrument,
                    opinion.market_regime.value,
                    opinion.direction.value,
                    opinion.conviction,
                    opinion.model_dump_json(),
                ),
            )
            self._conn.commit()

    def resolve_opinion(self, opinion_id: str, pnl: float, note: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE research_memory SET outcome_pnl=?, outcome_note=?, resolved_at=? "
                "WHERE opinion_id=?",
                (pnl, note, now_utc().isoformat(), opinion_id),
            )
            self._conn.commit()

    def research_context(self, underlying: str, regime: str, n: int = 5) -> str:
        """Resolved same-regime opinions first, most recent first — prompt context."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT direction, conviction, outcome_pnl, outcome_note, ts FROM research_memory "
                "WHERE underlying=? AND regime=? AND resolved_at IS NOT NULL "
                "ORDER BY ts DESC LIMIT ?",
                (underlying, regime, n),
            ).fetchall()
        if not rows:
            return ""
        lines = [
            f"- {ts}: said {direction} (c={conviction:.2f}) → pnl {pnl:+.0f}. {note}"
            for direction, conviction, pnl, note, ts in rows
        ]
        return "PAST RESOLVED OPINIONS (same regime):\n" + "\n".join(lines)

    # -- trading memory --------------------------------------------------------

    def store_trade(self, trade: TradeRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO trading_memory "
                "(trade_id, ts, strategy_id, regime, setup_digest, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    trade.trade_id,
                    trade.exit_ts.isoformat(),
                    trade.strategy_id,
                    trade.regime,
                    trade.setup_digest,
                    trade.model_dump_json(),
                ),
            )
            self._conn.commit()

    def similar_trades(
        self, strategy_id: str, regime: str, n: int = 10, before: datetime | None = None
    ) -> list[TradeRecord]:
        query = (
            "SELECT payload FROM trading_memory WHERE strategy_id=? AND regime=?"
        )
        args: list[str] = [strategy_id, regime]
        if before is not None:
            query += " AND ts < ?"
            args.append(ensure_utc(before).isoformat())
        query += " ORDER BY ts DESC LIMIT ?"
        args.append(str(n))
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [TradeRecord.model_validate(json.loads(r[0])) for r in rows]

    def strategy_stats(self, strategy_id: str, regime: str | None = None) -> dict:
        trades = self.similar_trades(strategy_id, regime, n=10_000) if regime else []
        if regime is None:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT payload FROM trading_memory WHERE strategy_id=?", (strategy_id,)
                ).fetchall()
            trades = [TradeRecord.model_validate(json.loads(r[0])) for r in rows]
        if not trades:
            return {"n": 0}
        pnls = [t.net_pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        return {
            "n": len(trades),
            "net_pnl": round(sum(pnls), 2),
            "win_rate": round(len(wins) / len(trades), 3),
            "expectancy": round(sum(pnls) / len(trades), 2),
            "worst": round(min(pnls), 2),
        }
