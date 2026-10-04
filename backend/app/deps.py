from __future__ import annotations

from datetime import UTC, datetime


def get_now() -> datetime:
    """Injectable clock (overridden in tests)."""
    return datetime.now(UTC)
