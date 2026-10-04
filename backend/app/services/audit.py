from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent

_FORBIDDEN_KEYS = {"value", "values", "samples", "token", "secret", "password"}


def audit(
    db: Session,
    actor: uuid.UUID | None,
    action: str,
    profile_id: uuid.UUID | None = None,
    **meta: Any,
) -> None:
    """Record a minimal audit event. Raw health values and secrets are rejected."""
    bad = _FORBIDDEN_KEYS.intersection(meta)
    if bad:
        raise ValueError(f"audit metadata must not contain {sorted(bad)}")
    db.add(AuditEvent(actor_user_id=actor, action=action, health_profile_id=profile_id, meta=meta))
