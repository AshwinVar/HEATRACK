from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user
from app.authz import ensure_profile_row, not_found, require_owner
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import get_now
from app.models import (
    CaregiverGrant,
    HealthProfile,
    Invitation,
    Profile,
)
from app.schemas import (
    GrantOut,
    HealthProfileCreate,
    HealthProfileUpdate,
    InvitationAccept,
    InvitationCreate,
    MeUpdate,
)
from app.services.audit import audit
from app.services.notify import cancel_for_grant
from app.services.rules import default_rules

router = APIRouter(prefix="/v1")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def profile_out(hp: HealthProfile, role: str, categories: list[str]) -> dict[str, Any]:
    return {
        "id": hp.id,
        "display_name": hp.display_name,
        "timezone": hp.timezone,
        "data_mode": hp.data_mode,
        "monitoring_enabled": hp.monitoring_enabled,
        "contact_phone": hp.contact_phone,
        "preferred_sources": hp.preferred_sources,
        "role": role,
        "categories": sorted(categories),
    }


def grant_out(db: Session, g: CaregiverGrant) -> GrantOut:
    cg = db.get(Profile, g.caregiver_user_id)
    hp = db.get(HealthProfile, g.health_profile_id)
    return GrantOut(
        id=g.id,
        health_profile_id=g.health_profile_id,
        caregiver_user_id=g.caregiver_user_id,
        caregiver_display_name=cg.display_name if cg else None,
        caregiver_email=cg.email if cg else None,
        wearer_display_name=hp.display_name if hp else None,
        status=g.status,
        categories=g.categories,
        consent_version=g.consent_version,
        created_at=g.created_at,
        confirmed_at=g.confirmed_at,
        revoked_at=g.revoked_at,
    )


@router.get("/me")
def me(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    p = ensure_profile_row(db, user)
    db.commit()
    owned = (
        db.execute(select(HealthProfile).where(HealthProfile.owner_user_id == user.id))
        .scalars()
        .all()
    )
    grants = (
        db.execute(
            select(CaregiverGrant).where(
                CaregiverGrant.caregiver_user_id == user.id, CaregiverGrant.status != "revoked"
            )
        )
        .scalars()
        .all()
    )
    return {
        "id": p.id,
        "email": p.email,
        "display_name": p.display_name,
        "timezone": p.timezone,
        "owned_health_profiles": [str(h.id) for h in owned],
        "caregiver_grants": [grant_out(db, g) for g in grants],
    }


@router.patch("/me")
def update_me(
    body: MeUpdate, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    p = ensure_profile_row(db, user)
    if body.display_name is not None:
        p.display_name = body.display_name
    if body.timezone is not None:
        p.timezone = body.timezone
    db.commit()
    return {"id": p.id, "display_name": p.display_name, "timezone": p.timezone}


@router.delete("/me", status_code=204)
def delete_me(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    """Delete ALL FamilyPulse data for the caller: owned health profiles (with measurements,
    alerts, grants and pending notifications), caregiver grants, push devices and the app
    profile. The Supabase Auth identity itself is deleted separately (Supabase dashboard or
    admin API with the service-role key, which this API deliberately does not hold)."""
    owned = db.execute(
        select(HealthProfile).where(HealthProfile.owner_user_id == user.id)
    ).scalars()
    for hp in owned:
        audit(db, user.id, "health_profile.delete", hp.id)
        db.delete(hp)
    p = db.get(Profile, user.id)
    if p is not None:
        db.delete(p)  # cascades caregiver grants, push devices, outbox rows
    audit(db, user.id, "account.delete")
    db.commit()
    return Response(status_code=204)


@router.post("/health-profiles", status_code=201)
def create_health_profile(
    body: HealthProfileCreate,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ensure_profile_row(db, user)
    existing = db.execute(
        select(HealthProfile).where(
            HealthProfile.owner_user_id == user.id, HealthProfile.data_mode == body.data_mode
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="profile_exists")
    hp = HealthProfile(
        owner_user_id=user.id,
        display_name=body.display_name,
        timezone=body.timezone,
        data_mode=body.data_mode,
        contact_phone=body.contact_phone,
        preferred_sources={},
        monitoring_enabled=True,
    )
    db.add(hp)
    db.flush()
    db.add_all(default_rules(hp, settings))
    audit(db, user.id, "health_profile.create", hp.id, data_mode=body.data_mode)
    db.commit()
    return profile_out(hp, "owner", [])


@router.get("/health-profiles")
def list_health_profiles(
    user: AuthUser = Depends(current_user), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    out = [
        profile_out(hp, "owner", [])
        for hp in db.execute(
            select(HealthProfile).where(HealthProfile.owner_user_id == user.id)
        ).scalars()
    ]
    rows = db.execute(
        select(HealthProfile, CaregiverGrant)
        .join(CaregiverGrant, CaregiverGrant.health_profile_id == HealthProfile.id)
        .where(CaregiverGrant.caregiver_user_id == user.id, CaregiverGrant.status == "active")
    ).all()
    out += [profile_out(hp, "caregiver", g.categories) for hp, g in rows]
    return out


@router.patch("/health-profiles/{profile_id}")
def update_health_profile(
    profile_id: uuid.UUID,
    body: HealthProfileUpdate,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    hp = require_owner(db, user, profile_id).profile
    for field in ("display_name", "timezone", "monitoring_enabled", "contact_phone"):
        v = getattr(body, field)
        if v is not None:
            setattr(hp, field, v)
    if body.preferred_sources is not None:
        hp.preferred_sources = {str(k): v for k, v in body.preferred_sources.items()}
    audit(db, user.id, "health_profile.update", hp.id)
    db.commit()
    return profile_out(hp, "owner", [])


@router.delete("/health-profiles/{profile_id}", status_code=204)
def delete_health_profile(
    profile_id: uuid.UUID, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    """Deletes all owned data. Grants, invitations, measurements, alerts and pending
    notifications cascade; the audit record keeps only the profile id."""
    hp = require_owner(db, user, profile_id, lock=True).profile
    db.delete(hp)
    audit(db, user.id, "health_profile.delete", profile_id)
    db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------- invitations & grants
@router.post("/health-profiles/{profile_id}/invitations", status_code=201)
def create_invitation(
    profile_id: uuid.UUID,
    body: InvitationCreate,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    hp = require_owner(db, user, profile_id).profile
    token = secrets.token_urlsafe(24)
    inv = Invitation(
        health_profile_id=hp.id,
        token_hash=_hash(token),
        categories=body.categories,
        created_by=user.id,
        expires_at=now + timedelta(minutes=settings.invitation_ttl_minutes),
    )
    db.add(inv)
    audit(db, user.id, "invitation.create", hp.id)
    db.commit()
    # The plaintext token is returned exactly once and never stored.
    return {
        "invitation_id": inv.id,
        "token": token,
        "expires_at": inv.expires_at,
        "categories": inv.categories,
    }


@router.post("/invitations/accept", status_code=201)
def accept_invitation(
    body: InvitationAccept,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    now: datetime = Depends(get_now),
) -> GrantOut:
    ensure_profile_row(db, user)
    inv = db.execute(
        select(Invitation).where(Invitation.token_hash == _hash(body.token)).with_for_update()
    ).scalar_one_or_none()
    if inv is None or inv.claimed_at is not None or inv.expires_at <= now:
        raise HTTPException(status.HTTP_410_GONE, detail="invitation_invalid_or_used")
    hp = db.get(HealthProfile, inv.health_profile_id)
    if hp is None:
        raise HTTPException(status.HTTP_410_GONE, detail="invitation_invalid_or_used")
    if hp.owner_user_id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="cannot_accept_own_invitation")
    existing = db.execute(
        select(CaregiverGrant).where(
            CaregiverGrant.health_profile_id == hp.id,
            CaregiverGrant.caregiver_user_id == user.id,
            CaregiverGrant.status != "revoked",
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="grant_exists")
    inv.claimed_by = user.id
    inv.claimed_at = now
    g = CaregiverGrant(
        health_profile_id=hp.id,
        caregiver_user_id=user.id,
        invitation_id=inv.id,
        status="pending",  # becomes active only after the wearer confirms identity
        categories=inv.categories,
        consent_version=settings.consent_version,
    )
    db.add(g)
    db.flush()
    audit(db, user.id, "invitation.accept", hp.id, grant_id=str(g.id))
    db.commit()
    return grant_out(db, g)


@router.get("/health-profiles/{profile_id}/grants")
def list_grants(
    profile_id: uuid.UUID, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)
) -> list[GrantOut]:
    require_owner(db, user, profile_id)
    rows = db.execute(
        select(CaregiverGrant)
        .where(CaregiverGrant.health_profile_id == profile_id)
        .order_by(CaregiverGrant.created_at.desc())
    ).scalars()
    return [grant_out(db, g) for g in rows]


def _owned_grant(db: Session, user: AuthUser, grant_id: uuid.UUID) -> CaregiverGrant:
    g = db.execute(
        select(CaregiverGrant).where(CaregiverGrant.id == grant_id).with_for_update()
    ).scalar_one_or_none()
    if g is None:
        raise not_found()
    hp = db.get(HealthProfile, g.health_profile_id)
    if hp is None or hp.owner_user_id != user.id:
        raise not_found()
    return g


@router.post("/grants/{grant_id}/confirm")
def confirm_grant(
    grant_id: uuid.UUID,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> GrantOut:
    g = _owned_grant(db, user, grant_id)
    if g.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"grant_{g.status}")
    g.status = "active"
    g.confirmed_at = now
    audit(db, user.id, "grant.confirm", g.health_profile_id, grant_id=str(g.id))
    db.commit()
    return grant_out(db, g)


@router.delete("/grants/{grant_id}")
def revoke_grant(
    grant_id: uuid.UUID,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> GrantOut:
    g = _owned_grant(db, user, grant_id)
    if g.status != "revoked":
        g.status = "revoked"
        g.revoked_at = now
        cancelled = cancel_for_grant(db, g.health_profile_id, g.caregiver_user_id, now)
        audit(
            db,
            user.id,
            "grant.revoke",
            g.health_profile_id,
            grant_id=str(g.id),
            cancelled_notifications=cancelled,
        )
        db.commit()
    return grant_out(db, g)
