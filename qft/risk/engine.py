"""The deterministic risk firewall.

Pure evaluation over (intent, snapshot, portfolio view, clock). The only
mutable state the engine itself keeps is what duplicate/frequency detection
requires (seen intents, order timestamps, in-flight orders); everything
monetary comes from the injected PortfolioView so decisions are reproducible.

Fail-closed everywhere: any missing/ambiguous input rejects the intent.
"""

from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from qft.config.risk_limits import RiskLimits
from qft.data.calendar import NSECalendar
from qft.domain.enums import Environment, KillSwitch, Side
from qft.domain.ids import new_id
from qft.domain.instruments import Instrument
from qft.domain.market import VerifiedMarketSnapshot
from qft.domain.risk import RiskDecision, RiskReason
from qft.domain.signals import TradeIntent
from qft.domain.time import IST, ensure_utc
from qft.risk.kill_switch import KillSwitchManager


class PortfolioView(BaseModel):
    """The ledger's answer to 'what is true right now' — inputs to the firewall."""

    model_config = ConfigDict(frozen=True)

    equity: float = Field(gt=0)
    cash_available: float = Field(ge=0)
    margin_available: float = Field(ge=0)
    high_water_mark: float = Field(gt=0)
    realized_pnl_today: float = 0.0
    realized_pnl_week: float = 0.0
    unrealized_pnl: float = 0.0
    open_position_count: int = Field(ge=0, default=0)
    open_underlyings: tuple[str, ...] = ()
    open_strategy_ids: tuple[str, ...] = ()
    orders_today: int = Field(ge=0, default=0)
    broker_connected: bool = True
    reconciled: bool = True


class RiskEngine:
    def __init__(
        self,
        limits: RiskLimits,
        calendar: NSECalendar,
        kill_switch: KillSwitchManager,
        environment: Environment,
        allowed_expiries_provider: "callable[[], list[date]] | None" = None,
        live_armed_check: "callable[[], bool] | None" = None,
    ) -> None:
        self._limits = limits
        self._calendar = calendar
        self._ks = kill_switch
        self._env = environment
        self._expiries_provider = allowed_expiries_provider
        self._live_armed = live_armed_check
        self._seen_intents: set[str] = set()
        self._inflight_orders: set[str] = set()
        self._order_times: deque[datetime] = deque(maxlen=512)

    # -- hooks the execution layer must call ---------------------------------

    def record_order_submitted(self, order_reference_id: str, ts: datetime) -> None:
        self._inflight_orders.add(order_reference_id)
        self._order_times.append(ensure_utc(ts))

    def record_order_terminal(self, order_reference_id: str) -> None:
        self._inflight_orders.discard(order_reference_id)

    # -- main entry -----------------------------------------------------------

    def evaluate(
        self,
        intent: TradeIntent,
        snapshot: VerifiedMarketSnapshot | None,
        portfolio: PortfolioView,
        instrument: Instrument | None,
        now: datetime,
        is_exit: bool = False,
    ) -> RiskDecision:
        now = ensure_utc(now)
        reasons: list[RiskReason] = []
        evaluated: list[str] = []
        detail_parts: list[str] = []

        def fail(reason: RiskReason, detail: str = "") -> None:
            if reason not in reasons:
                reasons.append(reason)
            if detail:
                detail_parts.append(detail)

        # 1. Kill switches -----------------------------------------------------
        evaluated.append("kill_switch")
        if self._ks.state is KillSwitch.HARD:
            fail(RiskReason.KILL_SWITCH_HARD, self._ks.reason)
        elif not is_exit and not self._ks.new_entries_allowed:
            fail(RiskReason.KILL_SWITCH_SOFT, self._ks.reason)

        if not self._ks.strategy_enabled(intent.strategy_id):
            evaluated.append("strategy_enabled")
            fail(RiskReason.STRATEGY_DISABLED, intent.strategy_id)

        # 2. Environment gate ---------------------------------------------------
        evaluated.append("environment_gate")
        if self._env is Environment.LIVE:
            armed = bool(self._live_armed and self._live_armed())
            if not armed:
                fail(RiskReason.ENV_NOT_ARMED, "LIVE not armed (restart always disarms)")

        # 3. Signal integrity ---------------------------------------------------
        evaluated.append("signal_integrity")
        if intent.signal_expiry <= now:
            fail(RiskReason.SIGNAL_EXPIRED, f"expired {intent.signal_expiry.isoformat()}")
        if intent.intent_id in self._seen_intents:
            fail(RiskReason.DUPLICATE_INTENT, intent.intent_id)
        if intent.intent_id in self._inflight_orders:
            fail(RiskReason.DUPLICATE_ORDER_IN_FLIGHT, intent.intent_id)

        # 4. Market state --------------------------------------------------------
        evaluated.append("market_state")
        ist = now.astimezone(IST)
        if not self._calendar.is_market_open(now):
            fail(RiskReason.MARKET_CLOSED)
        elif not is_exit:
            s = self._limits.session
            if not (s.entry_open_ist <= ist.time() <= s.entry_close_ist):
                fail(RiskReason.OUTSIDE_SESSION_WINDOW, ist.time().isoformat())
        if not is_exit and ist.date().isoformat() in self._limits.event_blackout_dates:
            fail(RiskReason.EVENT_BLACKOUT, ist.date().isoformat())

        # 5. Data integrity --------------------------------------------------------
        evaluated.append("data_integrity")
        quote = None
        if snapshot is None:
            fail(RiskReason.SNAPSHOT_MISSING)
        else:
            if not snapshot.verified:
                fail(RiskReason.SNAPSHOT_UNVERIFIED, "; ".join(snapshot.issues[:3]))
            age = (now - snapshot.as_of).total_seconds()
            if age > self._limits.max_snapshot_age_seconds:
                fail(RiskReason.SNAPSHOT_STALE, f"{age:.0f}s old")
            if snapshot.snapshot_id != intent.snapshot_id:
                fail(RiskReason.SNAPSHOT_MISMATCH,
                     f"intent={intent.snapshot_id} snap={snapshot.snapshot_id}")
            quote = snapshot.quote_for(intent.instrument_key)
            if quote is None:
                fail(RiskReason.SNAPSHOT_MISMATCH, f"no quote for {intent.instrument_key}")

        # 6. Instrument gates ---------------------------------------------------------
        evaluated.append("instrument_gates")
        if intent.underlying not in self._limits.instrument_allowlist:
            fail(RiskReason.INSTRUMENT_NOT_ALLOWED, intent.underlying)
        if instrument is None:
            fail(RiskReason.INSTRUMENT_NOT_ALLOWED, "instrument master row missing")
        else:
            if instrument.key != intent.instrument_key:
                fail(RiskReason.SNAPSHOT_MISMATCH, "instrument/master key mismatch")
            if intent.quantity != intent.lots * instrument.lot_size:
                fail(
                    RiskReason.LOT_SIZE_INVALID,
                    f"qty {intent.quantity} != {intent.lots} lots x {instrument.lot_size}",
                )
            if intent.lots > self._limits.max_lots_per_order:
                fail(RiskReason.LOT_SIZE_INVALID, f"lots {intent.lots} > max")
            if instrument.is_option and intent.expiry is not None:
                allowed = self._allowed_expiries(ist.date())
                if allowed is not None and intent.expiry not in allowed:
                    fail(RiskReason.EXPIRY_NOT_ALLOWED, str(intent.expiry))
            if (
                not self._limits.allow_short_options
                and instrument.is_option
                and intent.side is Side.SELL
                and not is_exit
            ):
                fail(RiskReason.NAKED_SHORT_OPTION_FORBIDDEN)

        # 7. Liquidity gates ----------------------------------------------------------
        evaluated.append("liquidity_gates")
        if quote is not None and instrument is not None:
            spread = quote.spread_pct
            cap = (
                self._limits.max_spread_pct_options
                if instrument.is_option
                else self._limits.max_spread_pct_futures
            )
            if spread is None:
                fail(RiskReason.LIQUIDITY_INSUFFICIENT, "one-sided/absent book")
            elif spread > cap:
                fail(RiskReason.SPREAD_TOO_WIDE, f"{spread:.4%} > {cap:.4%}")
            if instrument.is_option:
                oi = quote.open_interest
                if oi is None or oi < self._limits.min_open_interest:
                    fail(RiskReason.OI_TOO_LOW, f"oi={oi}")

        # 8-9. Capital & loss gates (entries only) ---------------------------------------
        if not is_exit:
            evaluated.append("capital_gates")
            premium_outlay = None
            if quote is not None and intent.side is Side.BUY:
                ref_price = intent.entry_price_limit or quote.ltp
                premium_outlay = ref_price * intent.quantity
                if premium_outlay > self._limits.max_premium_per_trade:
                    fail(
                        RiskReason.PREMIUM_CAP_EXCEEDED,
                        f"₹{premium_outlay:.0f} > ₹{self._limits.max_premium_per_trade:.0f}",
                    )
                if premium_outlay > portfolio.cash_available:
                    fail(
                        RiskReason.INSUFFICIENT_CAPITAL,
                        f"need ₹{premium_outlay:.0f}, cash ₹{portfolio.cash_available:.0f}",
                    )

            evaluated.append("loss_gates")
            per_trade_budget = portfolio.equity * self._limits.max_capital_at_risk_per_trade_pct
            if intent.estimated_max_loss > per_trade_budget:
                # This is exactly the "minimum lot exceeds risk budget" case when lots==1.
                reason = (
                    RiskReason.MIN_LOT_EXCEEDS_RISK_BUDGET
                    if intent.lots == 1
                    else RiskReason.PER_TRADE_LOSS_EXCEEDED
                )
                fail(reason, f"max_loss ₹{intent.estimated_max_loss:.0f} > budget ₹{per_trade_budget:.0f}")

            daily_budget = self._limits.max_daily_loss(portfolio.equity)
            daily_headroom = daily_budget + min(portfolio.realized_pnl_today, 0.0)
            if daily_headroom <= 0 or intent.estimated_max_loss > daily_headroom:
                fail(
                    RiskReason.DAILY_LOSS_LIMIT,
                    f"headroom ₹{daily_headroom:.0f} after today ₹{portfolio.realized_pnl_today:.0f}",
                )
            weekly_budget = self._limits.max_weekly_loss(portfolio.equity)
            weekly_headroom = weekly_budget + min(portfolio.realized_pnl_week, 0.0)
            if weekly_headroom <= 0 or intent.estimated_max_loss > weekly_headroom:
                fail(RiskReason.WEEKLY_LOSS_LIMIT, f"headroom ₹{weekly_headroom:.0f}")

            drawdown = (portfolio.high_water_mark - portfolio.equity) / portfolio.high_water_mark
            if drawdown >= self._limits.max_drawdown_halt_pct:
                fail(RiskReason.DRAWDOWN_HALT, f"drawdown {drawdown:.1%}")

            if intent.expected_reward_risk < self._limits.min_reward_risk:
                fail(
                    RiskReason.REWARD_RISK_TOO_LOW,
                    f"{intent.expected_reward_risk:.2f} < {self._limits.min_reward_risk:.2f}",
                )

            # 10. Exposure gates
            evaluated.append("exposure_gates")
            if portfolio.open_position_count >= self._limits.max_concurrent_positions:
                fail(RiskReason.MAX_CONCURRENT_POSITIONS, str(portfolio.open_position_count))
            if intent.strategy_id in portfolio.open_strategy_ids:
                fail(RiskReason.STRATEGY_EXPOSURE_LIMIT, intent.strategy_id)
            if intent.underlying in portfolio.open_underlyings:
                fail(RiskReason.UNDERLYING_EXPOSURE_LIMIT, intent.underlying)

            # 11. Frequency gates
            evaluated.append("frequency_gates")
            if portfolio.orders_today >= self._limits.max_orders_per_day:
                fail(RiskReason.DAILY_ORDER_LIMIT, str(portfolio.orders_today))
            window_start = now - timedelta(seconds=60)
            recent = sum(1 for t in self._order_times if t >= window_start)
            if recent >= self._limits.max_orders_per_minute:
                fail(RiskReason.ORDER_FREQUENCY_LIMIT, f"{recent}/min")

        # 12. Broker & reconciliation ------------------------------------------------------
        evaluated.append("broker_state")
        if not portfolio.broker_connected:
            fail(RiskReason.BROKER_DEGRADED)
        if not portfolio.reconciled:
            fail(RiskReason.RECONCILIATION_MISMATCH)

        approved = not reasons
        if approved:
            self._seen_intents.add(intent.intent_id)
        return RiskDecision(
            decision_id=new_id("rd"),
            intent_id=intent.intent_id,
            ts=now,
            approved=approved,
            reasons=(RiskReason.APPROVED,) if approved else tuple(reasons),
            evaluated_rules=tuple(evaluated),
            snapshot_id=snapshot.snapshot_id if snapshot else "",
            detail="; ".join(detail_parts),
        )

    # -- helpers ---------------------------------------------------------------

    def _allowed_expiries(self, today: date) -> set[date] | None:
        """Nearest N weekly expiries (+ current monthly when enabled).

        Returns None when no provider is wired (backtests inject their own);
        in that case the expiry gate is enforced by the provider used there.
        """
        if self._expiries_provider is None:
            return None
        upcoming = sorted(d for d in self._expiries_provider() if d >= today)
        allowed = set(upcoming[: self._limits.allowed_weekly_expiries])
        if self._limits.allow_monthly_expiry:
            month_expiries = [d for d in upcoming if d.month == today.month]
            if month_expiries:
                allowed.add(month_expiries[-1])
        return allowed
