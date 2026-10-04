from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, text

from app.config import get_settings
from app.models import AuditEvent, NotificationOutbox, PushDevice
from app.services.notify import dispatch_due
from app.services.push import FakeTransport
from app.worker import run_once
from tests.conftest import T0, batch
from tests.test_alerts import _alerts, enable_hr_high, hr_window, register_push


def _trigger(world, factory, fake):
    w, pid = world["wearer"], world["pid"]
    enable_hr_high(w, pid)
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(T0, [130] * 6, "h")]))
    return run_once(factory, get_settings(), fake, now=T0)


def _job(factory):
    with factory() as db:
        return db.scalars(select(NotificationOutbox)).one()


def test_failed_push_is_retried_with_backoff(world, factory):
    register_push(world["carer"])
    fake = FakeTransport(script=["retry", "retry"])
    res = _trigger(world, factory, fake)
    assert res["dispatch"] == {"retry": 1}
    job = _job(factory)
    assert job.attempts == 1 and job.accepted_at is None and job.next_attempt_at > T0
    # Not due yet: nothing happens.
    assert dispatch_due(factory, fake, get_settings(), T0 + timedelta(seconds=1)) == {}
    later = T0 + timedelta(hours=2)
    assert dispatch_due(factory, fake, get_settings(), later) == {"retry": 1}
    much_later = T0 + timedelta(hours=6)
    assert dispatch_due(factory, fake, get_settings(), much_later) == {"accepted": 1}
    job = _job(factory)
    assert job.attempts == 3 and job.accepted_at is not None and len(fake.sent) == 1
    # Alert remains available regardless of push outcome.
    assert len(_alerts(factory)) == 1


def test_invalid_token_disables_device(world, factory):
    register_push(world["carer"])
    fake = FakeTransport(script=["invalid_token"])
    res = _trigger(world, factory, fake)
    assert res["dispatch"] == {"invalid_token": 1}
    with factory() as db:
        dev = db.scalars(select(PushDevice)).one()
        assert dev.disabled_reason == "invalid_token"
    assert _job(factory).discard_reason == "invalid_token"


def test_token_rotation_reenables_device(world):
    c = world["carer"]
    d1 = register_push(c)
    d2 = c.post(
        "/v1/push-devices",
        {"installation_id": "carer-phone-001", "token": "rotated-token-xyz", "platform": "ios"},
    ).json()
    assert d1["id"] == d2["id"] and d2["disabled_at"] is None


def test_revoked_recipient_suppressed_via_cancellation(world, factory):
    register_push(world["carer"])
    fake = FakeTransport()
    # Evaluate without dispatching (transport=None) so the job stays queued.
    _trigger(world, factory, None)
    assert _job(factory).accepted_at is None
    world["wearer"].delete(f"/v1/grants/{world['grant_id']}")
    assert dispatch_due(factory, fake, get_settings(), T0) == {}
    assert fake.sent == []
    assert _job(factory).discard_reason == "grant_revoked"


def test_revoked_recipient_suppressed_at_send_time(world, factory):
    """Even if a job escaped cancellation, the grant is re-checked right before sending."""
    register_push(world["carer"])
    fake = FakeTransport()
    _trigger(world, factory, None)
    with factory() as db:  # revoke directly, bypassing the API's outbox cancellation
        db.execute(text("update caregiver_grants set status='revoked'"))
        db.commit()
    assert dispatch_due(factory, fake, get_settings(), T0) == {"discarded": 1}
    assert fake.sent == [] and _job(factory).discard_reason == "grant_not_active"


def test_claimed_jobs_are_not_double_sent(world, factory):
    register_push(world["carer"])
    _trigger(world, factory, None)
    from app.services.notify import claim_due

    with factory() as db:
        first = claim_due(db, T0, get_settings())
    with factory() as db:
        second = claim_due(db, T0, get_settings())
    assert len(first) == 1 and second == []  # leased


def test_test_notification_endpoint(world):
    from app.services import push

    c = world["carer"]
    d = register_push(c)
    fake = FakeTransport()
    push.set_transport(fake)
    try:
        r = c.post(f"/v1/push-devices/{d['id']}/test").json()
    finally:
        push.set_transport(None)
    assert r["transport"] == "fake" and r["provider_status"] == "accepted"
    assert "not proof" in r["note"]


def test_audit_log_has_no_health_values(world, factory):
    w, pid = world["wearer"], world["pid"]
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(T0, [123] * 3, "h")]))
    with factory() as db:
        metas = [e.meta for e in db.scalars(select(AuditEvent))]
    assert metas and "123" not in str(metas)


def test_rls_enabled_on_all_tables(factory):
    with factory() as db:
        rows = db.execute(
            text(
                "select relname, relrowsecurity from pg_class where relkind='r' "
                "and relnamespace='public'::regnamespace and relname <> 'alembic_version'"
            )
        ).all()
    assert rows and all(r[1] for r in rows)
