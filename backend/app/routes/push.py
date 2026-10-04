from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user
from app.authz import ensure_profile_row, not_found
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import get_now
from app.models import NotificationOutbox, PushDevice
from app.schemas import PushDeviceRegister
from app.services.push import PushMessage, get_transport

router = APIRouter(prefix="/v1")


def device_out(d: PushDevice) -> dict[str, Any]:
    return {
        "id": d.id,
        "installation_id": d.installation_id,
        "platform": d.platform,
        "last_registered_at": d.last_registered_at,
        "disabled_at": d.disabled_at,
        "disabled_reason": d.disabled_reason,
    }


@router.post("/push-devices", status_code=201)
def register_push_device(
    body: PushDeviceRegister,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Idempotent per installation; handles FCM token rotation by replacing the token."""
    ensure_profile_row(db, user)
    # A token can move between accounts on a shared phone: detach it from anyone else.
    db.execute(
        update(PushDevice)
        .where(
            PushDevice.token == body.token,
            PushDevice.user_id != user.id,
            PushDevice.disabled_at.is_(None),
        )
        .values(disabled_at=now, disabled_reason="token_reassigned")
    )
    d = db.execute(
        select(PushDevice).where(
            PushDevice.user_id == user.id, PushDevice.installation_id == body.installation_id
        )
    ).scalar_one_or_none()
    if d is None:
        d = PushDevice(user_id=user.id, installation_id=body.installation_id)
        db.add(d)
    d.token = body.token
    d.platform = body.platform
    d.last_registered_at = now
    d.disabled_at = None
    d.disabled_reason = None
    db.commit()
    return device_out(d)


@router.get("/push-devices")
def list_push_devices(
    user: AuthUser = Depends(current_user), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.execute(select(PushDevice).where(PushDevice.user_id == user.id)).scalars()
    return [device_out(d) for d in rows]


@router.delete("/push-devices/{device_id}", status_code=204)
def delete_push_device(
    device_id: uuid.UUID,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> Response:
    d = db.get(PushDevice, device_id)
    if d is None or d.user_id != user.id:
        raise not_found()
    db.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.push_device_id == d.id,
            NotificationOutbox.accepted_at.is_(None),
            NotificationOutbox.discarded_at.is_(None),
        )
        .values(discarded_at=now, discard_reason="device_removed")
    )
    db.delete(d)
    db.commit()
    return Response(status_code=204)


@router.post("/push-devices/{device_id}/test")
def test_push(
    device_id: uuid.UUID,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Send a test notification to one of the caller's own devices and report what the
    provider said. 'accepted' means FCM accepted it, not that the phone displayed it."""
    d = db.get(PushDevice, device_id)
    if d is None or d.user_id != user.id:
        raise not_found()
    transport = get_transport(settings)
    if transport is None:
        raise HTTPException(503, detail="push_not_configured")
    res = transport.send(
        PushMessage(
            token=d.token,
            platform=d.platform,
            title="FamilyPulse test",
            body="Test notification. No health data included.",
            data={"type": "test"},
        )
    )
    if res.status == "invalid_token":
        d.disabled_reason = "invalid_token"
        d.disabled_at = now
        db.commit()
    return {
        "transport": transport.name,
        "provider_status": res.status,
        "message_id": res.message_id,
        "error": res.error,
        "note": "Provider acceptance is not proof the phone displayed the notification.",
    }
