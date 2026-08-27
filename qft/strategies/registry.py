"""Strategy registry with per-strategy enable state."""

from __future__ import annotations

from qft.strategies.base import Strategy


class StrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        sid = strategy.spec.strategy_id
        if sid in self._strategies:
            raise ValueError(f"duplicate strategy id {sid}")
        self._strategies[sid] = strategy

    def get(self, strategy_id: str) -> Strategy:
        return self._strategies[strategy_id]

    def all(self) -> list[Strategy]:
        return list(self._strategies.values())
