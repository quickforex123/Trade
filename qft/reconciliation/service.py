"""Reconciliation: compare broker truth with the internal ledger.

Any unexplained mismatch trips the SOFT kill switch (no new entries) and marks
the portfolio unreconciled, which the risk firewall treats as a hard rejection
for every new intent. Local state NEVER overrides the broker.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from qft.brokers.base import BrokerAdapter
from qft.domain.enums import KillSwitch
from qft.domain.portfolio import LedgerEventType
from qft.portfolio.ledger import Ledger
from qft.risk.kill_switch import KillSwitchManager

logger = logging.getLogger(__name__)


class ReconciliationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    reconciled: bool
    mismatches: tuple[str, ...] = ()


class Reconciler:
    def __init__(self, broker: BrokerAdapter, ledger: Ledger, kill_switch: KillSwitchManager) -> None:
        self._broker = broker
        self._ledger = ledger
        self._ks = kill_switch

    def run(self) -> ReconciliationResult:
        mismatches: list[str] = []
        broker_positions = {p.trading_symbol: p for p in self._broker.positions()}
        ledger_positions = {
            p.trading_symbol: p for p in self._ledger.positions().values() if not p.is_flat
        }

        for symbol, bpos in broker_positions.items():
            lpos = ledger_positions.get(symbol)
            if lpos is None:
                mismatches.append(f"{symbol}: broker holds {bpos.net_quantity}, ledger flat")
            elif lpos.net_quantity != bpos.net_quantity:
                mismatches.append(
                    f"{symbol}: broker {bpos.net_quantity} != ledger {lpos.net_quantity}"
                )
        for symbol, lpos in ledger_positions.items():
            if symbol not in broker_positions:
                mismatches.append(f"{symbol}: ledger holds {lpos.net_quantity}, broker flat")

        reconciled = not mismatches
        self._ledger.append(
            LedgerEventType.RECONCILIATION,
            {"reconciled": reconciled, "mismatches": mismatches},
        )
        if not reconciled:
            logger.critical("RECONCILIATION MISMATCH: %s", "; ".join(mismatches))
            self._ks.trip(KillSwitch.SOFT, f"reconciliation mismatch: {mismatches[0]}")
        return ReconciliationResult(reconciled=reconciled, mismatches=tuple(mismatches))
