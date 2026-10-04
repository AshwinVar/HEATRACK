"""Outbox dispatcher.

Jobs are leased with SELECT ... FOR UPDATE SKIP LOCKED so several workers never double-claim.
Immediately before sending, the recipient's grant is re-read and locked FOR SHARE in the same
transaction as the send: a revocation (UPDATE of that row) either commits first, in which
case the job is discarded, or waits until this send is recorded. Nothing is sent after a
revocation has committed.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import (
    METRIC_CATEGORY,
    Alert,
    CaregiverGrant,
    HealthProfile,
    NotificationOutbox,
    PushDevice,
)
from app.services.push import PushMessage, PushTransport


def generic_message(alert: Alert, device: PushDevice) -> PushMessage:
    sim = alert.is_simulated
    return PushMessage(
        token=device.token,
        platform=device.platform,
        title="FamilyPulse [SIMULATION]" if sim else "FamilyPulse",
        body="A new alert is available. Open the app to view it."
        if alert.kind in ("threshold", "trend")
        else "A monitoring status alert is available. Open the app to view it.",
        # No vitals in the push payload; details are fetched after authentication.
        data={
            "type": "alert",
            "alert_id": str(alert.id),
            "health_profile_id": str(alert.health_profile_id),
            "simulated": "true" if sim else "false",
        },
    )


def claim_due(db: Session, now: datetime, settings: Settings, limit: int = 50) -> list[str]:
    rows = db.execute(
        text(
            """
            UPDATE notification_outbox SET leased_until = :lease
            WHERE id IN (
              SELECT id FROM notification_outbox
              WHERE accepted_at IS NULL AND discarded_at IS NULL
                AND next_attempt_at <= :now
                AND (leased_until IS NULL OR leased_until < :now)
              ORDER BY next_attempt_at
              LIMIT :limit
              FOR UPDATE SKIP LOCKED)
            RETURNING id
            """
        ),
        {"now": now, "lease": now + timedelta(seconds=settings.push_lease_seconds), "limit": limit},
    ).all()
    db.commit()
    return [str(r[0]) for r in rows]


def _discard(job: NotificationOutbox, now: datetime, reason: str) -> None:
    job.discarded_at = now
    job.discard_reason = reason
    job.leased_until = None


def backoff(settings: Settings, attempts: int) -> float:
    base = settings.push_backoff_base_seconds * (2 ** max(0, attempts - 1))
    return min(settings.push_backoff_max_seconds, base) + random.uniform(  # noqa: S311
        0, settings.push_backoff_base_seconds
    )


def process_job(
    db: Session, job_id: str, transport: PushTransport, settings: Settings, now: datetime
) -> str:
    job = db.execute(
        select(NotificationOutbox).where(NotificationOutbox.id == job_id).with_for_update()
    ).scalar_one_or_none()
    if job is None or job.accepted_at or job.discarded_at:
        db.rollback()
        return "skipped"
    alert = db.get(Alert, job.alert_id)
    device = db.get(PushDevice, job.push_device_id)
    hp = db.get(HealthProfile, alert.health_profile_id) if alert else None
    if alert is None or hp is None:
        _discard(job, now, "alert_gone")
        db.commit()
        return "discarded"
    needed = {"alerts"}
    needed.add(
        "connectivity"
        if alert.kind == "connectivity" or not alert.metric
        else METRIC_CATEGORY[alert.metric]
    )
    grant = db.execute(
        select(CaregiverGrant)
        .where(
            CaregiverGrant.health_profile_id == hp.id,
            CaregiverGrant.caregiver_user_id == job.recipient_user_id,
            CaregiverGrant.status == "active",
        )
        .with_for_update(read=True)  # FOR SHARE: blocks a concurrent revocation
    ).scalar_one_or_none()
    if grant is None or not needed.issubset(set(grant.categories)):
        _discard(job, now, "grant_not_active")
        db.commit()
        return "discarded"
    if device is None or device.disabled_at is not None or device.user_id != job.recipient_user_id:
        _discard(job, now, "device_disabled")
        db.commit()
        return "discarded"
    if job.sequence > 0 and alert.status != "open":
        _discard(job, now, "reminder_not_needed")
        db.commit()
        return "discarded"

    job.attempts += 1
    result = transport.send(generic_message(alert, device))
    if result.status == "accepted":
        job.accepted_at = now
        job.provider_message_id = result.message_id
        job.leased_until = None
        job.last_error = None
        outcome = "accepted"
    elif result.status == "invalid_token":
        device.disabled_at = now
        device.disabled_reason = "invalid_token"
        _discard(job, now, "invalid_token")
        job.last_error = result.error
        outcome = "invalid_token"
    else:
        job.last_error = (result.error or "error")[:255]
        if job.attempts >= settings.push_max_attempts:
            _discard(job, now, "max_attempts")
            outcome = "gave_up"
        else:
            job.next_attempt_at = now + timedelta(seconds=backoff(settings, job.attempts))
            job.leased_until = None
            outcome = "retry"
    db.commit()
    return outcome


def dispatch_due(
    factory: sessionmaker[Session], transport: PushTransport, settings: Settings, now: datetime
) -> dict[str, int]:
    counts: dict[str, int] = {}
    with factory() as db:
        ids = claim_due(db, now, settings)
    for job_id in ids:
        with factory() as db:
            outcome = process_job(db, job_id, transport, settings, now)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def cancel_for_grant(db: Session, profile_id: object, caregiver_id: object, now: datetime) -> int:
    """Discard pending jobs for a revoked caregiver (in the revocation transaction)."""
    alert_ids = select(Alert.id).where(Alert.health_profile_id == profile_id)
    res = db.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.recipient_user_id == caregiver_id,
            NotificationOutbox.alert_id.in_(alert_ids),
            NotificationOutbox.accepted_at.is_(None),
            NotificationOutbox.discarded_at.is_(None),
        )
        .values(discarded_at=now, discard_reason="grant_revoked")
    )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]
