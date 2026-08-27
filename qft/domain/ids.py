"""Identifier helpers.

`deterministic_id` exists so the same logical event (a signal fired by a
strategy at a bar, an order derived from an intent) always maps to the same
id — the backbone of duplicate detection and broker idempotency.
"""

from __future__ import annotations

import hashlib
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def deterministic_id(prefix: str, *parts: object) -> str:
    """Stable id from the given parts. Same parts -> same id, always."""
    if not parts:
        raise ValueError("deterministic_id requires at least one part")
    joined = "\x1f".join(str(p) for p in parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"
