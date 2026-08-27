"""Strategy framework: interface, registry, concrete research strategies."""

from qft.strategies.base import Strategy, StrategySpec
from qft.strategies.registry import StrategyRegistry

__all__ = ["Strategy", "StrategyRegistry", "StrategySpec"]
