"""qft command-line entry points.

Subcommands:
    backtest   — run a strategy over stored PIT bars (or synthetic demo data)
    dashboard  — serve the read-only dashboard
    status     — print ledger/kill-switch status
    arm-live   — create a LIVE arming record (requires the exact phrase)
    disarm     — remove the arming record

LIVE trading additionally requires the execution daemon process, environment
QFT_ENVIRONMENT=LIVE, and a fresh arming record; a restart always disarms.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from qft.config.risk_limits import load_risk_limits
from qft.config.settings import Settings
from qft.domain.time import now_utc
from qft.monitoring.logging_setup import setup_logging
from qft.risk.arming import ARM_PHRASE, LiveArming
from qft.risk.kill_switch import KillSwitchManager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qft")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="run a walk-forward backtest")
    bt.add_argument("--strategy", default="orb_v1", choices=["orb_v1", "vwap_trend_v1"])
    bt.add_argument("--synthetic-days", type=int, default=0,
                    help="use N synthetic days (mechanics demo only, NOT evidence)")
    bt.add_argument("--capital", type=float, default=500_000)

    dash = sub.add_parser("dashboard", help="serve the read-only dashboard")
    dash.add_argument("--port", type=int, default=None)

    sub.add_parser("status", help="print current status")

    arm = sub.add_parser("arm-live", help="arm LIVE trading (interactive, expiring)")
    arm.add_argument("--hours", type=float, default=2.0)

    sub.add_parser("disarm", help="disarm LIVE trading")

    args = parser.parse_args(argv)
    settings = Settings()
    setup_logging(settings.log_level)

    if args.command == "backtest":
        return _cmd_backtest(args)
    if args.command == "dashboard":
        return _cmd_dashboard(args, settings)
    if args.command == "status":
        return _cmd_status(settings)
    if args.command == "arm-live":
        return _cmd_arm(args, settings)
    if args.command == "disarm":
        LiveArming(settings.data_dir / "live_arm.json").disarm(operator=getpass.getuser())
        print("disarmed")
        return 0
    return 2


def _cmd_backtest(args: argparse.Namespace) -> int:
    from qft.backtest.engine import BacktestConfig, BacktestEngine
    from qft.backtest.evaluation import evaluate_strategy, walk_forward_days
    from qft.backtest.metrics import compute_metrics
    from qft.costs.model import CostModel
    from qft.features.engine import FeatureEngine
    from qft.regime.engine import RegimeEngine
    from qft.strategies.orb import OpeningRangeBreakout
    from qft.strategies.vwap_trend import VwapTrendContinuation

    strategy = OpeningRangeBreakout() if args.strategy == "orb_v1" else VwapTrendContinuation()

    if args.synthetic_days > 0:
        print("WARNING: synthetic data exercises MECHANICS only — results are NOT evidence "
              "of profitability and must never be used for promotion decisions.")
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tests.synthetic import synth_history

        history = synth_history(args.synthetic_days)
    else:
        print("No historical dataset wired yet: ingest real bars into the PIT store "
              "(qft.data.pit_store) and extend this command. Refusing to fabricate data.")
        return 1

    engine = BacktestEngine(
        strategy=strategy,
        feature_engine=FeatureEngine(),
        regime_engine=RegimeEngine(hysteresis_bars=1),
        cost_model=CostModel(),
        config=BacktestConfig(initial_capital=args.capital),
    )
    days = sorted(history)
    folds = walk_forward_days(days, n_folds=3)
    if not folds:
        print("not enough days for walk-forward")
        return 1
    is_results, oos_results = [], []
    for train, test in folds:
        is_results.append(engine.run({d: history[d] for d in train}))
        oos_results.append(engine.run({d: history[d] for d in test}))
    all_trades = [t for r in oos_results for t in r.trades]
    curve = [pt for r in oos_results for pt in r.equity_curve]
    metrics = compute_metrics(all_trades, curve, args.capital, sum(r.days for r in oos_results))
    ev = evaluate_strategy(strategy.spec.strategy_id, oos_results, is_results, args.capital)
    print(json.dumps({
        "strategy": strategy.spec.strategy_id,
        "oos_metrics": metrics.model_dump(),
        "eligible": ev.eligible,
        "gate_failures": ev.gate_failures,
        "score": ev.score,
        "components": ev.components,
    }, indent=2, default=str))
    return 0


def _cmd_dashboard(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    from qft.domain.enums import Environment
    from qft.monitoring.dashboard import create_dashboard
    from qft.portfolio.ledger import Ledger

    limits = load_risk_limits(settings.risk_config_path)
    ledger = Ledger(settings.ledger_path, settings.environment, limits.initial_capital)
    ks = KillSwitchManager(settings.data_dir / "kill_switch.json")
    app = create_dashboard(ledger, ks, Environment(settings.environment), limits.initial_capital)
    uvicorn.run(app, host=settings.dashboard_host, port=args.port or settings.dashboard_port)
    return 0


def _cmd_status(settings: Settings) -> int:
    from qft.portfolio.ledger import Ledger

    limits = load_risk_limits(settings.risk_config_path)
    ks = KillSwitchManager(settings.data_dir / "kill_switch.json")
    arming = LiveArming(settings.data_dir / "live_arm.json")
    if settings.ledger_path.exists():
        ledger = Ledger(settings.ledger_path, settings.environment, limits.initial_capital)
        view = ledger.portfolio_view(now_utc())
        state = {
            "environment": settings.environment.value,
            "equity": view.equity,
            "pnl_today": view.realized_pnl_today,
            "open_positions": view.open_position_count,
            "orders_today": view.orders_today,
        }
    else:
        state = {"environment": settings.environment.value, "ledger": "not initialized"}
    state["kill_switch"] = ks.state.value
    state["live_armed"] = arming.is_armed()
    print(json.dumps(state, indent=2, default=str))
    return 0


def _cmd_arm(args: argparse.Namespace, settings: Settings) -> int:
    print("LIVE ARMING — this enables real-money order placement for the current process life.")
    print(f'Type the exact phrase to proceed:\n  {ARM_PHRASE}')
    phrase = input("> ")
    arming = LiveArming(settings.data_dir / "live_arm.json", max_arm_hours=args.hours)
    try:
        arming.arm(phrase, operator=getpass.getuser())
    except ValueError as e:
        print(f"refused: {e}")
        return 1
    print(f"armed for {args.hours}h — NOTE: only the process that armed stays armed; "
          "any restart disarms.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
