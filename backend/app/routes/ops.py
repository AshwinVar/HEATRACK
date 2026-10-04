from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime, timedelta
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import get_now
from app.schemas import DevTokenRequest

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, detail="database_unavailable") from exc
    return {"status": "ready"}


@router.get("/v1/diagnostics")
def diagnostics(
    _: AuthUser = Depends(current_user), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Non-sensitive server configuration so the app can show what is real and what is not."""
    return {
        "app_env": settings.app_env,
        "push_transport": settings.push_transport,
        "push_transport_label": {
            "fcm": "Firebase Cloud Messaging",
            "fake": "FAKE transport (development only - no phone receives anything)",
            "disabled": "Push not configured - alerts are visible in the app only",
        }[settings.push_transport],
        "auth": "jwks"
        if settings.auth_jwks_url
        else "hs256"
        if settings.auth_jwt_secret
        else "dev-mint"
        if settings.dev_token_mint_enabled
        else "unconfigured",
        "freshness_budgets_seconds": {
            m: settings.freshness_budget(m)
            for m in ("heart_rate", "resting_heart_rate", "steps", "sleep", "hrv_rmssd")
        },
        "heartbeat_stale_seconds": settings.heartbeat_stale_s,
        "consent_version": settings.consent_version,
    }


dev_router = APIRouter(prefix="/v1/dev")


@dev_router.post("/token")
def mint_dev_token(
    body: DevTokenRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """DEVELOPMENT ONLY. Mounted only when FP_APP_ENV=development and
    FP_DEV_TOKEN_MINT_ENABLED=true, and only answers clients in FP_DEV_TOKEN_ALLOWED_CIDRS
    (loopback by default). Tokens are still fully verified by the normal path
    (signature, issuer, audience, expiry, subject)."""
    host = request.client.host if request.client else ""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    nets = [ipaddress.ip_network(c) for c in settings.dev_token_allowed_cidrs]
    if addr is None or not any(addr in n for n in nets):
        raise HTTPException(403, detail="dev_token_network_not_allowed")
    assert settings.dev_jwt_secret
    sub = body.user_id or uuid.uuid4()
    token = jwt.encode(
        {
            "sub": str(sub),
            "email": body.email,
            "role": "authenticated",
            "aud": settings.auth_audience,
            "iss": settings.auth_issuer,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=12)).timestamp()),
        },
        settings.dev_jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "user_id": str(sub), "dev_only": True}
