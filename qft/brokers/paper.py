"""PaperBroker: SimulatedBroker tuned for live-data paper trading.

Same fill mechanics as SimulatedBroker, plus stochastic-but-seeded friction
(occasional partial fills) so paper results don't flatter execution quality.
Chaos switches remain available for drills.
"""

from __future__ import annotations

import random

from qft.brokers.simulated import SimulatedBroker
from qft.domain.orders import ApprovedOrder, BrokerAck


class PaperBroker(SimulatedBroker):
    def __init__(
        self,
        slippage_pct: float = 0.001,
        partial_fill_prob: float = 0.10,
        seed: int = 20260827,
    ) -> None:
        super().__init__(slippage_pct=slippage_pct)
        self._rng = random.Random(seed)
        self._partial_prob = partial_fill_prob

    def place(self, order: ApprovedOrder) -> BrokerAck:
        if self._rng.random() < self._partial_prob:
            self.failures.partial_fill_next += 1
        return super().place(order)
