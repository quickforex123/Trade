"""Read-only dashboard over the ledger and runtime state.

Serves JSON endpoints plus a minimal HTML view. Strictly read-only: it holds
no broker credentials and cannot place, modify, or cancel anything.
"""

from __future__ import annotations

import html
from datetime import timedelta

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from qft.domain.enums import Environment
from qft.domain.portfolio import LedgerEventType
from qft.domain.time import IST, now_utc
from qft.portfolio.ledger import Ledger
from qft.risk.kill_switch import KillSwitchManager


def create_dashboard(
    ledger: Ledger,
    kill_switch: KillSwitchManager,
    environment: Environment,
    initial_capital: float,
) -> FastAPI:
    app = FastAPI(title="QFT dashboard", docs_url=None, redoc_url=None)

    def _state() -> dict:
        now = now_utc()
        view = ledger.portfolio_view(now)
        positions = [p.model_dump(mode="json") for p in ledger.positions().values() if not p.is_flat]
        recent_cut = now - timedelta(hours=24)
        decisions = ledger.events(LedgerEventType.RISK_DECISION, since=recent_cut)
        rejected = [
            {
                "intent_id": d["intent_id"],
                "ts": d["ts"],
                "reasons": d["payload"].get("reasons", []),
                "detail": d["payload"].get("detail", ""),
            }
            for d in decisions
            if not d["payload"].get("approved")
        ]
        intents = ledger.events(LedgerEventType.TRADE_INTENT, since=recent_cut)
        opinions = ledger.events(LedgerEventType.RESEARCH_OPINION, since=recent_cut)
        snapshots = ledger.events(LedgerEventType.SNAPSHOT, since=recent_cut)
        return {
            "environment": environment.value,
            "as_of": now.isoformat(),
            "as_of_ist": now.astimezone(IST).isoformat(),
            "kill_switch": kill_switch.state.value,
            "kill_switch_reason": kill_switch.reason,
            "disabled_strategies": sorted(kill_switch.disabled_strategies),
            "equity": view.equity,
            "initial_capital": initial_capital,
            "cash_available": view.cash_available,
            "realized_pnl_today": view.realized_pnl_today,
            "realized_pnl_week": view.realized_pnl_week,
            "drawdown_pct": round(
                (view.high_water_mark - view.equity) / view.high_water_mark * 100, 3
            ),
            "high_water_mark": view.high_water_mark,
            "open_positions": positions,
            "orders_today": view.orders_today,
            "reconciled": view.reconciled,
            "recent_intents": [i["payload"] for i in intents[-20:]],
            "recent_rejections": rejected[-20:],
            "latest_opinion": opinions[-1]["payload"] if opinions else None,
            "latest_snapshot": snapshots[-1]["payload"] if snapshots else None,
        }

    @app.get("/api/state")
    def api_state() -> dict:
        return _state()

    @app.get("/api/events")
    def api_events(event_type: str | None = None, hours: int = 24) -> list[dict]:
        et = LedgerEventType(event_type) if event_type else None
        since = now_utc() - timedelta(hours=min(hours, 24 * 30))
        return ledger.events(et, since=since)[-500:]

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        s = _state()
        pos_rows = "".join(
            f"<tr><td>{html.escape(p['trading_symbol'])}</td><td>{p['net_quantity']}</td>"
            f"<td>{p['average_price']:.2f}</td><td>{p['realized_pnl']:.2f}</td></tr>"
            for p in s["open_positions"]
        ) or "<tr><td colspan=4>flat</td></tr>"
        rej_rows = "".join(
            f"<tr><td>{html.escape(str(r['ts']))}</td><td>{html.escape(str(r['reasons']))}</td>"
            f"<td>{html.escape(str(r['detail']))}</td></tr>"
            for r in s["recent_rejections"]
        ) or "<tr><td colspan=3>none</td></tr>"
        ks_color = {"NONE": "#2e7d32", "SOFT": "#e65100", "HARD": "#b71c1c"}[s["kill_switch"]]
        return f"""<!doctype html><html><head><title>QFT</title>
<meta http-equiv="refresh" content="10">
<style>body{{font-family:ui-monospace,monospace;margin:2rem;background:#111;color:#ddd}}
table{{border-collapse:collapse;margin:1rem 0}}td,th{{border:1px solid #444;padding:4px 10px}}
.k{{color:#888}}.v{{color:#fff}}h2{{color:#8ab4f8}}</style></head><body>
<h1>QFT — {s['environment']}</h1>
<p><span class=k>kill switch:</span> <b style="color:{ks_color}">{s['kill_switch']}</b>
 {html.escape(s['kill_switch_reason'])}</p>
<p><span class=k>equity:</span> <span class=v>₹{s['equity']:.2f}</span>
 <span class=k>today:</span> {s['realized_pnl_today']:+.2f}
 <span class=k>week:</span> {s['realized_pnl_week']:+.2f}
 <span class=k>drawdown:</span> {s['drawdown_pct']:.2f}%
 <span class=k>orders today:</span> {s['orders_today']}
 <span class=k>reconciled:</span> {s['reconciled']}</p>
<h2>Open positions</h2>
<table><tr><th>symbol</th><th>qty</th><th>avg</th><th>realized</th></tr>{pos_rows}</table>
<h2>Recent rejections (risk reason codes)</h2>
<table><tr><th>ts</th><th>reasons</th><th>detail</th></tr>{rej_rows}</table>
<p class=k>as of {s['as_of_ist']} (auto-refresh 10s) — read-only view</p>
</body></html>"""

    return app
