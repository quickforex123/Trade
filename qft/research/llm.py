"""LLM client with enforced structured output.

One attempt per call; a response that fails schema validation returns None
(fail closed). There is deliberately NO free-text fallback — an unparseable
opinion is a discarded opinion, never prose smuggled downstream (the upstream
TradingAgents fallback was audited as a hazard and removed).
"""

from __future__ import annotations

import logging
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class StructuredLLM(Protocol):
    def generate(self, system: str, prompt: str, schema: type[T]) -> T | None: ...


class AnthropicLLM:
    """Claude client using forced tool-use for schema-valid output."""

    def __init__(self, model: str, max_tokens: int = 2048, timeout: float = 120.0) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._client = None  # lazy: no import/key needed unless research is enabled

    def _ensure_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(timeout=self._timeout)
        return self._client

    def generate(self, system: str, prompt: str, schema: type[T]) -> T | None:
        client = self._ensure_client()
        tool = {
            "name": "emit",
            "description": "Emit the structured result.",
            "input_schema": schema.model_json_schema(),
        }
        try:
            msg = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit"},
            )
        except Exception as e:  # provider/network errors: fail closed
            logger.error("LLM call failed (%s): %s", schema.__name__, type(e).__name__)
            return None
        for block in msg.content:
            if getattr(block, "type", "") == "tool_use" and block.name == "emit":
                try:
                    return schema.model_validate(block.input)
                except ValidationError as e:
                    logger.error("LLM output failed %s validation: %s", schema.__name__, e)
                    return None
        logger.error("LLM returned no tool_use block for %s", schema.__name__)
        return None


class FakeLLM:
    """Deterministic stand-in for tests: returns queued responses per schema."""

    def __init__(self) -> None:
        self._queues: dict[str, list[dict | None]] = {}
        self.calls: list[tuple[str, str]] = []

    def queue(self, schema: type[BaseModel], payload: dict | None) -> None:
        self._queues.setdefault(schema.__name__, []).append(payload)

    def generate(self, system: str, prompt: str, schema: type[T]) -> T | None:
        self.calls.append((schema.__name__, prompt))
        queue = self._queues.get(schema.__name__, [])
        payload = queue.pop(0) if queue else None
        if payload is None:
            return None
        try:
            return schema.model_validate(payload)
        except ValidationError:
            return None
