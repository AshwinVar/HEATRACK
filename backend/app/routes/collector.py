from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user
from app.authz import not_found, require_owner
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import get_now
from app.models import CheckIn, CollectorDevice, HealthProfile, SyncRun
from app.schemas import CheckInOut, CollectorRegister, HeartbeatIn, IngestBatchIn, IngestResult
from app.services.audit import audit
from app.services.ingest import ingest_batch

router = APIRouter(prefix="/v1")


def collector_out(c: CollectorDevice) -> dict[str, Any]:
    return {
        "id": c.id,
        "health_profile_id": c.health_profile_id,
        "installation_id": c.installation_id,
        "platform": c.platform,
        "capabilities": c.capabilities,
        "last_upload_at": c.last_upload_at,
        "last_heartbeat_at": c.last_heartbeat_at,
        "disabled_at": c.disabled_at,
        "created_at": c.created_at,
    }


@router.post("/collector-devices", status_code=201)
def register_collector(
    body: CollectorRegister,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    hp = require_owner(db, user, body.health_profile_id).profile
    c = db.execute(
        select(CollectorDevice).where(
            CollectorDevice.health_profile_id == hp.id,
            CollectorDevice.installation_id == body.installation_id,
        )
    ).scalar_one_or_none()
    if c is None:
        c = CollectorDevice(
            health_profile_id=hp.id,
            owner_user_id=user.id,
            installation_id=body.installation_id,
            platform=body.platform,
            capabilities=body.capabilities,
        )
        db.add(c)
    else:
        c.capabilities = body.capabilities
        c.disabled_at = None
    db.flush()
    audit(db, user.id, "collector.register", hp.id, collector_id=str(c.id))
    db.commit()
    return collector_out(c)


def _owned_collector(db: Session, user: AuthUser, device_id: uuid.UUID) -> CollectorDevice:
    c = db.get(CollectorDevice, device_id)
    if c is None or c.owner_user_id != user.id:
        raise not_found()
    hp = db.get(HealthProfile, c.health_profile_id)
    if hp is None or hp.owner_user_id != user.id:
        raise not_found()
    return c


@router.post("/collector-devices/{device_id}/heartbeat")
def heartbeat(
    device_id: uuid.UUID,
    body: HeartbeatIn,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Liveness of the collector only. Never refreshes any metric's freshness."""
    c = _owned_collector(db, user, device_id)
    if c.disabled_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="collector_disabled")
    c.last_heartbeat_at = now
    if body.capabilities is not None:
        c.capabilities = body.capabilities
    if body.sync_run is not None:
        r = body.sync_run
        db.add(
            SyncRun(
                collector_device_id=c.id,
                started_at=r.started_at,
                ended_at=r.ended_at,
                status=r.status,
                trigger=r.trigger,
                metrics_found=dict(r.metrics_found),
                checkpoint=r.checkpoint,
                error_category=r.error_category,
            )
        )
    db.commit()
    return {"ok": True, "last_heartbeat_at": c.last_heartbeat_at}


@router.delete("/collector-devices/{device_id}", status_code=200)
def disable_collector(
    device_id: uuid.UUID,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    c = _owned_collector(db, user, device_id)
    c.disabled_at = now
    audit(db, user.id, "collector.disable", c.health_profile_id, collector_id=str(c.id))
    db.commit()
    return collector_out(c)


@router.get("/health-profiles/{profile_id}/sync-runs")
def list_sync_runs(
    profile_id: uuid.UUID, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    require_owner(db, user, profile_id)
    rows = db.execute(
        select(SyncRun)
        .join(CollectorDevice, CollectorDevice.id == SyncRun.collector_device_id)
        .where(CollectorDevice.health_profile_id == profile_id)
        .order_by(SyncRun.started_at.desc())
        .limit(50)
    ).scalars()
    return [
        {
            "started_at": r.started_at,
            "ended_at": r.ended_at,
            "status": r.status,
            "trigger": r.trigger,
            "metrics_found": r.metrics_found,
            "error_category": r.error_category,
        }
        for r in rows
    ]


@router.post("/health-profiles/{profile_id}/ingest")
def ingest(
    profile_id: uuid.UUID,
    body: IngestBatchIn,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    now: datetime = Depends(get_now),
) -> IngestResult:
    return ingest_batch(db, user, profile_id, body, settings, now)


@router.post("/health-profiles/{profile_id}/check-ins", status_code=201)
def check_in(
    profile_id: uuid.UUID,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> CheckInOut:
    hp = require_owner(db, user, profile_id).profile
    ci = CheckIn(health_profile_id=hp.id, checked_in_at=now)
    db.add(ci)
    db.commit()
    return CheckInOut(id=ci.id, checked_in_at=ci.checked_in_at)
