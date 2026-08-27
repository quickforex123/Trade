"""Kill-switch state with restart-safe persistence.

The persisted file keeps the MOST RESTRICTIVE state across restarts. Arming
for LIVE is handled separately (qft.risk.arming) and always resets on restart;
kill switches deliberately do not.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from qft.domain.enums import KillSwitch
from qft.domain.time import now_utc

logger = logging.getLogger(__name__)

_ORDER = {KillSwitch.NONE: 0, KillSwitch.SOFT: 1, KillSwitch.HARD: 2}


class KillSwitchManager:
    def __init__(self, state_path: Path | str | None = None) -> None:
        self._path = Path(state_path) if state_path else None
        self._state = KillSwitch.NONE
        self._reason = ""
        self._disabled_strategies: set[str] = set()
        self._entries_disabled = False
        if self._path is not None and self._path.exists():
            self._load()

    # -- queries ------------------------------------------------------------

    @property
    def state(self) -> KillSwitch:
        return self._state

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def new_entries_allowed(self) -> bool:
        return self._state is KillSwitch.NONE and not self._entries_disabled

    @property
    def exits_allowed(self) -> bool:
        return self._state is not KillSwitch.HARD

    def strategy_enabled(self, strategy_id: str) -> bool:
        return strategy_id not in self._disabled_strategies

    @property
    def disabled_strategies(self) -> frozenset[str]:
        return frozenset(self._disabled_strategies)

    # -- transitions ----------------------------------------------------------

    def trip(self, level: KillSwitch, reason: str) -> None:
        """Escalate only — a trip can never lower the current level."""
        if _ORDER[level] > _ORDER[self._state]:
            logger.critical("KILL SWITCH %s: %s", level, reason)
            self._state = level
            self._reason = reason
            self._save()
        elif level is not KillSwitch.NONE:
            logger.warning("kill switch already at %s; trip(%s, %s) kept", self._state, level, reason)

    def clear(self, operator: str, note: str) -> None:
        """Operator-only de-escalation, audit-logged."""
        logger.warning("kill switch cleared by %s: %s (was %s: %s)",
                       operator, note, self._state, self._reason)
        self._state = KillSwitch.NONE
        self._reason = ""
        self._save()

    def disable_strategy(self, strategy_id: str, reason: str) -> None:
        logger.error("strategy %s disabled: %s", strategy_id, reason)
        self._disabled_strategies.add(strategy_id)
        self._save()

    def enable_strategy(self, strategy_id: str, operator: str) -> None:
        logger.warning("strategy %s re-enabled by %s", strategy_id, operator)
        self._disabled_strategies.discard(strategy_id)
        self._save()

    def disable_new_entries(self, reason: str) -> None:
        logger.error("new entries disabled: %s", reason)
        self._entries_disabled = True
        self._save()

    def enable_new_entries(self, operator: str) -> None:
        logger.warning("new entries re-enabled by %s", operator)
        self._entries_disabled = False
        self._save()

    # -- persistence ----------------------------------------------------------

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {
            "state": self._state.value,
            "reason": self._reason,
            "disabled_strategies": sorted(self._disabled_strategies),
            "entries_disabled": self._entries_disabled,
            "saved_at": now_utc().isoformat(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def _load(self) -> None:
        assert self._path is not None
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            self._state = KillSwitch(payload.get("state", "NONE"))
            self._reason = str(payload.get("reason", ""))
            self._disabled_strategies = set(payload.get("disabled_strategies", []))
            self._entries_disabled = bool(payload.get("entries_disabled", False))
            if self._state is not KillSwitch.NONE:
                logger.critical(
                    "restored kill switch %s from disk (%s) — most restrictive state survives restart",
                    self._state, self._reason,
                )
        except (ValueError, KeyError, json.JSONDecodeError):
            # Corrupt state file: fail closed.
            logger.critical("kill-switch state file unreadable — failing closed to HARD")
            self._state = KillSwitch.HARD
            self._reason = "corrupt kill-switch state file"
