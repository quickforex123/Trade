"""LIVE arming mechanism.

LIVE trading requires an explicit, recent, operator-created arming record.
The record lives in MEMORY plus an expiring on-disk token that must have been
created AFTER this process started — so a restart always disarms, by
construction, even though the file survives.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from qft.domain.time import now_utc

logger = logging.getLogger(__name__)

ARM_PHRASE = "ARM LIVE TRADING I ACCEPT THE RISK"


class LiveArming:
    def __init__(self, token_path: Path | str, max_arm_hours: float = 6.0) -> None:
        self._path = Path(token_path)
        self._max_hours = max_arm_hours
        self._process_start = now_utc()
        self._armed_in_memory = False

    def arm(self, phrase: str, operator: str) -> None:
        if phrase != ARM_PHRASE:
            raise ValueError("arming phrase mismatch — refusing to arm")
        payload = {
            "armed_at": now_utc().isoformat(),
            "operator": operator,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._path)
        self._armed_in_memory = True
        logger.critical("LIVE ARMED by %s until +%.1fh or restart", operator, self._max_hours)

    def disarm(self, operator: str = "system") -> None:
        self._armed_in_memory = False
        if self._path.exists():
            self._path.unlink()
        logger.critical("LIVE DISARMED by %s", operator)

    def is_armed(self) -> bool:
        """Armed iff: armed in this process's memory AND the token exists AND
        the token was written after this process started AND it hasn't expired."""
        if not self._armed_in_memory or not self._path.exists():
            return False
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            from datetime import datetime, timedelta

            armed_at = datetime.fromisoformat(payload["armed_at"])
        except (ValueError, KeyError, json.JSONDecodeError):
            return False
        now = now_utc()
        if armed_at < self._process_start:
            return False  # stale token from a previous life — restart disarms
        if now - armed_at > timedelta(hours=self._max_hours):
            return False
        return True
