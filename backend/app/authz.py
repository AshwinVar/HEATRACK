"""Authorization. Profile IDs and client roles are untrusted: access is derived only from
ownership or an *active* category-scoped caregiver grant, re-read on every request."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthUser
from app.models import GRANT_CATEGORIES, METRIC_CATEGORY, CaregiverGrant, HealthProfile, Profile


@dataclass(frozen=True)
class Access:
    profile: HealthProfile
    role: str  # owner | caregiver
    categories: frozenset[str] = field(default_factory=frozenset)
    grant_id: uuid.UUID | None = None

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    def can(self, category: str) -> bool:
        return self.is_owner or category in self.categories

    def can_metric(self, metric: str) -> bool:
        return self.can(METRIC_CATEGORY[metric])


def not_found() -> HTTPException:
    # Same response for "does not exist" and "not authorized" to avoid leaking existence.
    return HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")


def ensure_profile_row(db: Session, user: AuthUser) -> Profile:
    profile = db.get(Profile, user.id)
    if profile is None:
        profile = Profile(id=user.id, email=user.email, display_name="", timezone="UTC")
        db.add(profile)
        db.flush()
    elif user.email and profile.email != user.email:
        profile.email = user.email
    return profile


def get_access(db: Session, user: AuthUser, profile_id: uuid.UUID, *, lock: bool = False) -> Access:
    stmt = select(HealthProfile).where(HealthProfile.id == profile_id)
    if lock:
        stmt = stmt.with_for_update()
    hp = db.execute(stmt).scalar_one_or_none()
    if hp is None:
        raise not_found()
    if hp.owner_user_id == user.id:
        return Access(profile=hp, role="owner", categories=frozenset(GRANT_CATEGORIES))
    grant = db.execute(
        select(CaregiverGrant).where(
            CaregiverGrant.health_profile_id == profile_id,
            CaregiverGrant.caregiver_user_id == user.id,
            CaregiverGrant.status == "active",
        )
    ).scalar_one_or_none()
    if grant is None:
        raise not_found()
    return Access(
        profile=hp, role="caregiver", categories=frozenset(grant.categories), grant_id=grant.id
    )


def require_owner(db: Session, user: AuthUser, profile_id: uuid.UUID, **kw: bool) -> Access:
    access = get_access(db, user, profile_id, **kw)
    if not access.is_owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="owner_only")
    return access


def require_category(access: Access, category: str) -> None:
    if not access.can(category):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"grant_missing_{category}")
