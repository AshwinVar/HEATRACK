from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user
from app.authz import Access, get_access, not_found, require_category, require_owner
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import get_now
from app.models import (
    METRIC_CATEGORY,
    METRICS,
    Alert,
    CheckIn,
    HealthProfile,
    Measurement,
    NotificationOutbox,
    Rule,
)
from app.routes.accounts import profile_out
from app.schemas import Metric, RuleUpdate
from app.services.audit import audit
from app.services.freshness import (
    UNAVAILABLE_METRICS,
    active_collectors,
    all_metric_statuses,
    daily_summaries,
)
from app.services.rules import threshold_active, trend_status

router = APIRouter(prefix="/v1")

NO_ANOMALY_TEXT = "No configured anomalies detected in available data"


def _alert_visible(access: Access, a: Alert) -> bool:
    if not access.can("alerts"):
        return False
    if a.kind in ("connectivity",) or not a.metric:
        return access.can("connectivity")
    if a.kind == "data_availability":
        return access.can("connectivity") or access.can_metric(a.metric)
    return access.can_metric(a.metric)


def alert_out(db: Session, a: Alert, *, detail: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": a.id,
        "health_profile_id": a.health_profile_id,
        "kind": a.kind,
        "metric": a.metric,
        "category": a.category,
        "status": a.status,
        "reason": a.reason,
        "is_simulated": a.is_simulated,
        "opened_at": a.opened_at,
        "acknowledged_at": a.acknowledged_at,
        "resolved_at": a.resolved_at,
        "resolution_reason": a.resolution_reason,
        "rule_config_version": a.rule_config_version,
        "wearer_timezone": hp.timezone
        if (hp := db.get(HealthProfile, a.health_profile_id))
        else "UTC",
    }
    if detail:
        out["evidence"] = a.evidence
        jobs = (
            db.execute(select(NotificationOutbox).where(NotificationOutbox.alert_id == a.id))
            .scalars()
            .all()
        )
        out["notifications"] = {
            "queued": sum(1 for j in jobs if not j.accepted_at and not j.discarded_at),
            # Provider acceptance only; not proof of delivery or that anyone saw it.
            "accepted_by_provider": sum(1 for j in jobs if j.accepted_at),
            "discarded": sum(1 for j in jobs if j.discarded_at),
        }
    return out


@router.get("/health-profiles/{profile_id}/dashboard")
def dashboard(
    profile_id: uuid.UUID,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    access = get_access(db, user, profile_id)
    hp = access.profile
    allowed = {m for m in METRICS if access.can_metric(m)}
    metrics = all_metric_statuses(db, hp, settings, now, allowed)
    daily = daily_summaries(db, hp, now, 7, allowed)
    alerts = [
        a
        for a in db.execute(
            select(Alert).where(Alert.health_profile_id == hp.id, Alert.status != "resolved")
        ).scalars()
        if _alert_visible(access, a)
    ]
    collector: dict[str, Any] | None = None
    if access.can("connectivity"):
        devices = active_collectors(db, hp.id)
        hb_limit = timedelta(seconds=settings.heartbeat_stale_s)
        collector = {
            "devices": [
                {
                    "id": c.id,
                    "platform": c.platform,
                    "last_heartbeat_at": c.last_heartbeat_at,
                    "last_upload_at": c.last_upload_at,
                    "heartbeat_state": (
                        "never"
                        if c.last_heartbeat_at is None
                        else "ok"
                        if now - c.last_heartbeat_at <= hb_limit
                        else "late"
                    ),
                    "background_sync": (c.capabilities or {}).get("background_read"),
                    "health_connect": (c.capabilities or {}).get("health_connect"),
                }
                for c in devices
            ],
            "last_check_in_at": db.execute(
                select(func.max(CheckIn.checked_in_at)).where(CheckIn.health_profile_id == hp.id)
            ).scalar_one_or_none(),
        }
    insights = []
    rules = db.execute(select(Rule).where(Rule.health_profile_id == hp.id)).scalars().all()
    for r in rules:
        if r.kind == "trend" and r.enabled and r.metric in allowed:
            insights.append(trend_status(db, hp, r, now))
    any_live_threshold = any(r.kind == "threshold" and threshold_active(r, hp) for r in rules)
    has_current = any(m["state"] == "fresh" for m in metrics)
    if alerts:
        summary = f"{len(alerts)} open alert(s)"
    elif not has_current:
        summary = "No current data available"
    else:
        summary = NO_ANOMALY_TEXT
    return {
        "profile": profile_out(hp, access.role, sorted(access.categories)),
        "generated_at": now,
        "data_mode": hp.data_mode,
        "banner": "SIMULATED DATA - not real measurements"
        if hp.data_mode == "simulation"
        else None,
        "summary_text": summary,
        "health_thresholds_configured": any_live_threshold,
        "metrics": metrics,
        "unavailable_metrics": UNAVAILABLE_METRICS,
        "daily": daily,
        "collector": collector,
        "open_alerts": [alert_out(db, a) for a in alerts],
        "insights": insights,
    }


def _encode_cursor(t: datetime, mid: int) -> str:
    return base64.urlsafe_b64encode(f"{t.isoformat()}|{mid}".encode()).decode()


def _decode_cursor(c: str) -> tuple[datetime, int]:
    try:
        t, mid = base64.urlsafe_b64decode(c.encode()).decode().split("|")
        return datetime.fromisoformat(t), int(mid)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="bad_cursor") from exc


@router.get("/health-profiles/{profile_id}/measurements")
def measurements(
    profile_id: uuid.UUID,
    metric: Metric,
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    cursor: str | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access = get_access(db, user, profile_id)
    require_category(access, METRIC_CATEGORY[metric])
    if from_.tzinfo is None or to.tzinfo is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="timezone_required")
    if to <= from_ or to - from_ > timedelta(days=93):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="bad_range")
    q = select(Measurement).where(
        Measurement.health_profile_id == profile_id,
        Measurement.metric == metric,
        Measurement.measured_at >= from_,
        Measurement.measured_at < to,
    )
    if cursor:
        ct, cid = _decode_cursor(cursor)
        q = q.where(
            or_(
                Measurement.measured_at > ct,
                and_(Measurement.measured_at == ct, Measurement.id > cid),
            )
        )
    rows = list(
        db.execute(q.order_by(Measurement.measured_at, Measurement.id).limit(limit + 1)).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "metric": metric,
        "data_mode": access.profile.data_mode,
        "timezone": access.profile.timezone,
        "points": [
            {
                "t": m.measured_at,
                "end": m.end_at,
                "value": m.value,
                "unit": m.unit,
                "kind": m.kind,
                "origin": m.data_origin,
                "received_at": m.received_at,
                "simulated": m.is_simulated,
                "backfill": m.is_backfill,
            }
            for m in rows
        ],
        "next_cursor": _encode_cursor(rows[-1].measured_at, rows[-1].id) if more else None,
    }


@router.get("/health-profiles/{profile_id}/alerts")
def list_alerts(
    profile_id: uuid.UUID,
    state: str | None = Query(default=None, pattern="^(open|acknowledged|resolved|unresolved)$"),
    limit: int = Query(default=100, ge=1, le=500),
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    access = get_access(db, user, profile_id)
    require_category(access, "alerts")
    q = select(Alert).where(Alert.health_profile_id == profile_id)
    if state == "unresolved":
        q = q.where(Alert.status != "resolved")
    elif state:
        q = q.where(Alert.status == state)
    rows = db.execute(q.order_by(Alert.opened_at.desc()).limit(limit)).scalars()
    return [alert_out(db, a) for a in rows if _alert_visible(access, a)]


def _visible_alert(db: Session, user: AuthUser, alert_id: uuid.UUID) -> tuple[Alert, Access]:
    a = db.get(Alert, alert_id)
    if a is None:
        raise not_found()
    access = get_access(db, user, a.health_profile_id)
    if not _alert_visible(access, a):
        raise not_found()
    return a, access


@router.get("/alerts/{alert_id}")
def get_alert(
    alert_id: uuid.UUID, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    a, _ = _visible_alert(db, user, alert_id)
    return alert_out(db, a, detail=True)


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge(
    alert_id: uuid.UUID,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    a, _ = _visible_alert(db, user, alert_id)
    if a.status == "open":
        # Acknowledging records that a person saw it. It never resolves the condition.
        a.status = "acknowledged"
        a.acknowledged_at = now
        a.acknowledged_by = user.id
        audit(db, user.id, "alert.acknowledge", a.health_profile_id, alert_id=str(a.id))
        db.commit()
    return alert_out(db, a, detail=True)


def rule_out(r: Rule) -> dict[str, Any]:
    return {
        c: getattr(r, c)
        for c in (
            "id",
            "kind",
            "metric",
            "enabled",
            "direction",
            "threshold_value",
            "recovery_value",
            "window_minutes",
            "min_samples",
            "max_gap_minutes",
            "freshness_minutes",
            "cooldown_minutes",
            "reminder_minutes",
            "config_version",
            "baseline_min_days",
            "baseline_lookback_days",
            "deviation_threshold",
            "deviation_floor",
            "is_synthetic_demo",
            "reviewed_at",
        )
    }


@router.get("/health-profiles/{profile_id}/rules")
def list_rules(
    profile_id: uuid.UUID, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    access = get_access(db, user, profile_id)
    require_category(access, "alerts")
    rows = db.execute(
        select(Rule).where(Rule.health_profile_id == profile_id).order_by(Rule.kind, Rule.metric)
    ).scalars()
    return [rule_out(r) for r in rows]


@router.put("/health-profiles/{profile_id}/rules/{rule_id}")
def update_rule(
    profile_id: uuid.UUID,
    rule_id: uuid.UUID,
    body: RuleUpdate,
    user: AuthUser = Depends(current_user),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    hp = require_owner(db, user, profile_id).profile
    r = db.get(Rule, rule_id)
    if r is None or r.health_profile_id != hp.id:
        raise not_found()
    changes = body.model_dump(exclude_unset=True, exclude={"reviewed_for_wearer"})
    for k, v in changes.items():
        setattr(r, k, v)
    if r.kind == "threshold" and r.enabled:
        if r.threshold_value is None or r.direction not in ("above", "below"):
            raise HTTPException(422, detail="threshold_and_direction_required")
        if r.recovery_value is not None and (
            (r.direction == "above" and r.recovery_value > r.threshold_value)
            or (r.direction == "below" and r.recovery_value < r.threshold_value)
        ):
            raise HTTPException(422, detail="recovery_value_must_be_on_safe_side")
        if hp.data_mode == "simulation":
            if not r.is_synthetic_demo:
                raise HTTPException(422, detail="simulation_rules_must_be_synthetic_demo")
        else:
            if r.is_synthetic_demo:
                raise HTTPException(422, detail="synthetic_demo_rules_not_allowed_on_live")
            if body.reviewed_for_wearer:
                r.reviewed_at = now
            elif "threshold_value" in changes or "direction" in changes or r.reviewed_at is None:
                raise HTTPException(422, detail="threshold_requires_review_for_wearer")
    if hp.data_mode == "live" and r.is_synthetic_demo:
        raise HTTPException(422, detail="synthetic_demo_rules_not_allowed_on_live")
    r.config_version += 1
    r.updated_at = now
    audit(db, user.id, "rule.update", hp.id, rule_id=str(r.id), version=r.config_version)
    db.commit()
    return rule_out(r)
