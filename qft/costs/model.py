"""Deterministic Indian F&O transaction-cost model.

Rates are configuration reviewed against the current schedules (broker tariff,
STT/CTT, NSE transaction charges, SEBI turnover fee, stamp duty, GST). The
defaults below reflect the schedule as of early 2026 for NSE index derivatives
via a flat-fee discount structure (Groww): verify before LIVE.

All reward/objective computations in this platform use NET returns produced
through this model.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from qft.domain.enums import Side


class CostRates(BaseModel):
    model_config = ConfigDict(frozen=True)

    brokerage_flat_per_order: float = Field(ge=0, default=20.0)
    brokerage_pct_cap: float = Field(ge=0, default=0.0005)  # min(flat, pct·turnover) structures

    # STT: options — 0.1% on premium, SELL side; futures — 0.02% SELL side (2026 schedule)
    stt_option_sell_pct: float = Field(ge=0, default=0.001)
    stt_future_sell_pct: float = Field(ge=0, default=0.0002)

    # NSE exchange transaction charges (on premium for options / turnover for futures)
    exch_txn_option_pct: float = Field(ge=0, default=0.0003503)
    exch_txn_future_pct: float = Field(ge=0, default=0.0000173)

    sebi_fee_pct: float = Field(ge=0, default=0.000001)  # ₹10 / crore
    stamp_duty_option_buy_pct: float = Field(ge=0, default=0.00003)
    stamp_duty_future_buy_pct: float = Field(ge=0, default=0.00002)
    gst_pct: float = Field(ge=0, default=0.18)  # on brokerage + exchange txn + SEBI fee
    ipft_pct: float = Field(ge=0, default=0.000005)  # NSE investor protection fund (options)


class CostBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True)

    turnover: float
    brokerage: float
    stt: float
    exchange_txn: float
    sebi_fee: float
    stamp_duty: float
    ipft: float
    gst: float

    @property
    def total(self) -> float:
        return round(
            self.brokerage
            + self.stt
            + self.exchange_txn
            + self.sebi_fee
            + self.stamp_duty
            + self.ipft
            + self.gst,
            2,
        )


class CostModel:
    def __init__(self, rates: CostRates | None = None) -> None:
        self.rates = rates or CostRates()

    def option_leg(self, side: Side, premium: float, quantity: int) -> CostBreakdown:
        """Costs for one executed option leg (premium = per-unit price)."""
        if premium < 0 or quantity <= 0:
            raise ValueError("invalid premium/quantity")
        r = self.rates
        turnover = premium * quantity
        brokerage = min(r.brokerage_flat_per_order, r.brokerage_pct_cap * turnover)
        # Groww-style flat structures charge the flat fee; keep the min() so a
        # tiny order is never overcharged relative to the pct cap.
        brokerage = round(brokerage, 2)
        stt = round(turnover * r.stt_option_sell_pct, 2) if side is Side.SELL else 0.0
        exch = round(turnover * r.exch_txn_option_pct, 2)
        sebi = round(turnover * r.sebi_fee_pct, 2)
        stamp = round(turnover * r.stamp_duty_option_buy_pct, 2) if side is Side.BUY else 0.0
        ipft = round(turnover * r.ipft_pct, 2)
        gst = round((brokerage + exch + sebi) * r.gst_pct, 2)
        return CostBreakdown(
            turnover=turnover,
            brokerage=brokerage,
            stt=stt,
            exchange_txn=exch,
            sebi_fee=sebi,
            stamp_duty=stamp,
            ipft=ipft,
            gst=gst,
        )

    def future_leg(self, side: Side, price: float, quantity: int) -> CostBreakdown:
        if price <= 0 or quantity <= 0:
            raise ValueError("invalid price/quantity")
        r = self.rates
        turnover = price * quantity
        brokerage = round(min(r.brokerage_flat_per_order, r.brokerage_pct_cap * turnover), 2)
        stt = round(turnover * r.stt_future_sell_pct, 2) if side is Side.SELL else 0.0
        exch = round(turnover * r.exch_txn_future_pct, 2)
        sebi = round(turnover * r.sebi_fee_pct, 2)
        stamp = round(turnover * r.stamp_duty_future_buy_pct, 2) if side is Side.BUY else 0.0
        gst = round((brokerage + exch + sebi) * r.gst_pct, 2)
        return CostBreakdown(
            turnover=turnover,
            brokerage=brokerage,
            stt=stt,
            exchange_txn=exch,
            sebi_fee=sebi,
            stamp_duty=stamp,
            ipft=0.0,
            gst=gst,
        )

    def option_round_trip(
        self, entry_side: Side, entry_premium: float, exit_premium: float, quantity: int
    ) -> float:
        """Total costs for entering and exiting one option position."""
        exit_side = Side.SELL if entry_side is Side.BUY else Side.BUY
        return round(
            self.option_leg(entry_side, entry_premium, quantity).total
            + self.option_leg(exit_side, exit_premium, quantity).total,
            2,
        )
