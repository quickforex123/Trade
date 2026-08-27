"""Strategy interface.

A strategy declares its identity, constraints and cost assumptions up front
(StrategySpec) and emits Signals from features — it never sizes positions
beyond its declared risk rule, never talks to a broker, and never sees an
LLM. Structurally forbidden behaviours (naked short options, martingale,
averaging down) are excluded by the risk firewall regardless of what a
strategy emits.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict, Field

from qft.domain.enums import Regime
from qft.domain.market import VerifiedMarketSnapshot
from qft.domain.signals import Signal
from qft.features.engine import FeatureFrame


class StrategySpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy_id: str
    version: str
    description: str = ""
    allowed_underlyings: tuple[str, ...] = ("NIFTY",)
    allowed_regimes: tuple[Regime, ...]
    max_holding_minutes: int = Field(gt=0, default=120)
    stop_loss_atr_multiple: float = Field(gt=0, default=1.0)
    target_atr_multiple: float = Field(gt=0, default=2.0)
    required_min_oi: float = Field(ge=0, default=1_500_000)
    assumed_slippage_pct: float = Field(ge=0, default=0.005)
    cooldown_minutes: int = Field(ge=0, default=30)


class Strategy(ABC):
    """Stateless between bars except via explicitly persisted state."""

    spec: StrategySpec

    @abstractmethod
    def generate(
        self,
        frame: FeatureFrame,
        regime: Regime,
        snapshot: VerifiedMarketSnapshot | None,
    ) -> Signal | None:
        """Return a Signal or None. None (no trade) is a first-class outcome."""

    def permitted(self, regime: Regime, underlying: str) -> bool:
        return regime in self.spec.allowed_regimes and underlying in self.spec.allowed_underlyings
