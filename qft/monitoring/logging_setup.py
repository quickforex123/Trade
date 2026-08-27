"""Structured logging with secret scrubbing.

The scrubbing filter is a hard backstop against the leak class found in the
upstream audit (API keys inside URLs/exception strings reaching logs).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_SECRET_PATTERNS = [
    re.compile(r"(api[_-]?key=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(secret=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(sk-ant-)[A-Za-z0-9\-_]+"),
]


def scrub(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(r"\1[REDACTED]", text)
    return text


class ScrubbingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = scrub(repr(record.exc_info[1]))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ScrubbingJsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
