from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.models import Alert, NotificationOutbox
from app.services.push import FakeTransport
from app.worker import run_once
from tests.conftest import T0, User, batch, daily_record, hr_record


def _rule(user, pid, kind, metric=None, direction=None):
    for r in user.get(f"/v1/health-profiles/{pid}/rules").json():
        if (
            r["kind"] == kind
            and r["metric"] == metric
            and (direction is None or r["direction"] == direction)
        ):
            return r
    raise AssertionError("rule not found")


def enable_hr_high(w, pid, threshold=120, recovery=110):
    r = _rule(w, pid, "threshold", "heart_rate", "above")
    resp = w.put(
        f"/v1/health-profiles/{pid}/rules/{r['id']}",
        {
            "enabled": True,
            "threshold_value": threshold,
            "recovery_value": recovery,
            "window_minutes": 10,
            "min_samples": 5,
            "max_gap_minutes": 3,
            "freshness_minutes": 3,
            "reviewed_for_wearer": True,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def register_push(user, install="carer-phone-001"):
    return user.post(
        "/v1/push-devices",
        {"installation_id": install, "token": "fcm-token-" + install, "platform": "ios"},
    ).json()


def hr_window(end, values, rid):
    return hr_record(rid, end - timedelta(minutes=2 * (len(values) - 1)), values, step_minutes=2)


def _alerts(factory):
    with factory() as db:
        return db.scalars(select(Alert).order_by(Alert.opened_at)).all()


def _jobs(factory):
    with factory() as db:
        return db.scalars(select(NotificationOutbox)).all()


def test_health_thresholds_disabled_by_default_and_require_review(world, factory, clock):
    w, pid = world["wearer"], world["pid"]
    rules = w.get(f"/v1/health-profiles/{pid}/rules").json()
    assert not any(r["enabled"] for r in rules if r["kind"] in ("threshold", "trend"))
    r = _rule(w, pid, "threshold", "heart_rate", "above")
    resp = w.put(
        f"/v1/health-profiles/{pid}/rules/{r['id']}", {"enabled": True, "threshold_value": 120}
    )
    assert resp.json()["detail"] == "threshold_requires_review_for_wearer"
    resp = w.put(
        f"/v1/health-profiles/{pid}/rules/{r['id']}",
        {
            "enabled": True,
            "threshold_value": 120,
            "is_synthetic_demo": True,
            "reviewed_for_wearer": True,
        },
    )
    assert resp.json()["detail"] == "synthetic_demo_rules_not_allowed_on_live"
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(T0, [150] * 6, "h")]))
    run_once(factory, get_settings(), None, now=T0)
    assert _alerts(factory) == []


def test_persistent_episode_produces_one_alert_and_one_notification(world, factory, clock):
    w, c, pid = world["wearer"], world["carer"], world["pid"]
    enable_hr_high(w, pid)
    register_push(c)
    fake = FakeTransport()
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(T0, [130] * 6, "h1")]))
    res = run_once(factory, get_settings(), fake, now=T0)
    assert res["alerts_created"] == 1 and res["dispatch"] == {"accepted": 1}
    # Condition persists: re-evaluating creates neither a new alert nor a new push.
    t = clock.now = T0 + timedelta(minutes=2)
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(t, [131] * 6, "h2")]))
    res = run_once(factory, get_settings(), fake, now=t)
    assert res["alerts_created"] == 0 and res["dispatch"] == {}
    alerts = _alerts(factory)
    assert len(alerts) == 1 and len(_jobs(factory)) == 1 and len(fake.sent) == 1
    msg = fake.sent[0]
    # Generic push text: no vitals.
    assert "130" not in msg.body and "bpm" not in msg.body.lower()
    assert msg.data["alert_id"] == str(alerts[0].id)
    a = c.get(f"/v1/alerts/{alerts[0].id}").json()
    assert "not a diagnosis" in a["reason"] and "Activity context is unknown" in a["reason"]
    assert a["notifications"]["accepted_by_provider"] == 1


def test_insufficient_or_gappy_data_does_not_alert(world, factory, clock):
    w, pid = world["wearer"], world["pid"]
    enable_hr_high(w, pid)
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(T0, [150] * 3, "h1")]))
    run_once(factory, get_settings(), None, now=T0)
    assert _alerts(factory) == []  # only 3 samples (< min 5)
    # 6 high samples but with a 5-minute gap (> max_gap 3)
    t = clock.advance(minutes=60)
    rec = hr_record("h2", t - timedelta(minutes=9), [150, 150, 150], step_minutes=1)
    rec2 = hr_record("h3", t - timedelta(minutes=2), [150, 150, 150], step_minutes=1)
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([rec, rec2]))
    run_once(factory, get_settings(), None, now=t)
    assert _alerts(factory) == []


def test_mixed_values_are_not_persistent(world, factory):
    w, pid = world["wearer"], world["pid"]
    enable_hr_high(w, pid)
    w.post(
        f"/v1/health-profiles/{pid}/ingest",
        batch([hr_window(T0, [130, 130, 100, 130, 130, 130], "h1")]),
    )
    run_once(factory, get_settings(), None, now=T0)
    assert _alerts(factory) == []


def test_historical_backfill_never_creates_live_alert(world, factory):
    w, pid = world["wearer"], world["pid"]
    enable_hr_high(w, pid)
    # Old high values (3 days ago) and a backfill batch that happens to be recent.
    old = hr_window(T0 - timedelta(days=3), [150] * 6, "old")
    recent_backfill = hr_window(T0, [150] * 6, "bf")
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([old]))
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([recent_backfill], mode="backfill"))
    run_once(factory, get_settings(), None, now=T0)
    assert _alerts(factory) == []


def test_ongoing_condition_after_acknowledgement(world, factory, clock):
    w, c, pid = world["wearer"], world["carer"], world["pid"]
    enable_hr_high(w, pid)
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(T0, [130] * 6, "h1")]))
    run_once(factory, get_settings(), None, now=T0)
    alert_id = _alerts(factory)[0].id
    r = c.post(f"/v1/alerts/{alert_id}/acknowledge").json()
    assert r["status"] == "acknowledged" and r["resolved_at"] is None
    t = clock.now = T0 + timedelta(minutes=4)
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(t, [135] * 6, "h2")]))
    run_once(factory, get_settings(), None, now=t)
    alerts = _alerts(factory)
    assert len(alerts) == 1 and alerts[0].status == "acknowledged"
    # Hysteresis: 115 is below 120 but above the 110 recovery value -> still not resolved.
    t = clock.now = T0 + timedelta(minutes=14)
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(t, [115] * 6, "h3")]))
    run_once(factory, get_settings(), None, now=t)
    assert _alerts(factory)[0].status == "acknowledged"
    t = clock.now = T0 + timedelta(minutes=26)
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(t, [80] * 6, "h4")]))
    run_once(factory, get_settings(), None, now=t)
    a = _alerts(factory)[0]
    assert a.status == "resolved" and a.resolution_reason == "recovered"


def test_cooldown_suppresses_repeat_push_but_keeps_alert(world, factory, clock):
    w, c, pid = world["wearer"], world["carer"], world["pid"]
    enable_hr_high(w, pid)
    register_push(c)
    fake = FakeTransport()
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(T0, [130] * 6, "a")]))
    run_once(factory, get_settings(), fake, now=T0)
    t = clock.now = T0 + timedelta(minutes=12)
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(t, [80] * 6, "b")]))
    run_once(factory, get_settings(), fake, now=t)
    t = clock.now = T0 + timedelta(minutes=24)
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(t, [130] * 6, "c")]))
    run_once(factory, get_settings(), fake, now=t)
    alerts = _alerts(factory)
    assert [a.status for a in alerts] == ["resolved", "open"]
    assert alerts[1].evidence["notification"] == "suppressed_by_cooldown"
    assert len(fake.sent) == 1


def _seed_resting_hr(w, pid, days, value_fn, *, start_offset=2):
    recs = []
    for i in range(days):
        day = T0 - timedelta(days=start_offset + i)
        recs.append(
            daily_record("RestingHeartRateRecord", f"rhr-{i}", day.replace(hour=2), value_fn(i))
        )
    w.post(f"/v1/health-profiles/{pid}/ingest", batch(recs, mode="backfill"))


def _enable_trend(w, pid):
    r = _rule(w, pid, "trend", "resting_heart_rate")
    assert w.put(f"/v1/health-profiles/{pid}/rules/{r['id']}", {"enabled": True}).status_code == 200


def test_trend_insufficient_baseline(world, factory):
    w, c, pid = world["wearer"], world["carer"], world["pid"]
    _enable_trend(w, pid)
    _seed_resting_hr(w, pid, 5, lambda i: 62)
    w.post(
        f"/v1/health-profiles/{pid}/ingest",
        batch(
            [
                daily_record(
                    "RestingHeartRateRecord",
                    "rhr-target",
                    (T0 - timedelta(days=1)).replace(hour=2),
                    90,
                )
            ]
        ),
    )
    run_once(factory, get_settings(), None, now=T0)
    assert _alerts(factory) == []
    ins = c.get(f"/v1/health-profiles/{pid}/dashboard").json()["insights"][0]
    assert ins["state"] == "insufficient_baseline"
    assert ins["valid_prior_days"] == 5 and ins["required_days"] == 14


def test_trend_with_baseline_excludes_target_day(world, factory):
    w, c, pid = world["wearer"], world["carer"], world["pid"]
    _enable_trend(w, pid)
    _seed_resting_hr(w, pid, 20, lambda i: 60 + (i % 3))  # 60..62
    w.post(
        f"/v1/health-profiles/{pid}/ingest",
        batch(
            [
                daily_record(
                    "RestingHeartRateRecord",
                    "rhr-target",
                    (T0 - timedelta(days=1)).replace(hour=2),
                    75,
                )
            ]
        ),
    )
    run_once(factory, get_settings(), None, now=T0)
    ins = c.get(f"/v1/health-profiles/{pid}/dashboard").json()["insights"][0]
    assert ins["state"] == "evaluated" and ins["baseline_median"] == 61
    assert ins["valid_prior_days"] == 20  # target day not in baseline
    assert "not a probability" in ins["note"]
    alerts = _alerts(factory)
    assert len(alerts) == 1 and alerts[0].category == "insight"
    run_once(factory, get_settings(), None, now=T0 + timedelta(minutes=5))
    assert len(_alerts(factory)) == 1


def test_backfilled_target_day_never_alerts(world, factory):
    w, pid = world["wearer"], world["pid"]
    _enable_trend(w, pid)
    _seed_resting_hr(w, pid, 20, lambda i: 61)
    w.post(
        f"/v1/health-profiles/{pid}/ingest",
        batch(
            [
                daily_record(
                    "RestingHeartRateRecord",
                    "rhr-target",
                    (T0 - timedelta(days=1)).replace(hour=2),
                    90,
                )
            ],
            mode="backfill",
        ),
    )
    run_once(factory, get_settings(), None, now=T0)
    assert _alerts(factory) == []


def test_connectivity_and_data_availability_alerts(world, factory, clock):
    w, pid = world["wearer"], world["pid"]
    cid = world["collector_id"]
    w.post(f"/v1/collector-devices/{cid}/heartbeat", {})
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_window(T0, [70] * 3, "h")]))
    run_once(factory, get_settings(), None, now=T0)
    assert _alerts(factory) == []
    # 4h later heartbeat is fine but no HR: data-availability alert for HR only.
    t = clock.advance(hours=4)
    w.post(f"/v1/collector-devices/{cid}/heartbeat", {})
    run_once(factory, get_settings(), None, now=t)
    kinds = {(a.kind, a.metric) for a in _alerts(factory)}
    assert ("data_availability", "heart_rate") in kinds
    assert all(k != "connectivity" for k, _ in kinds)
    # 7h without heartbeat -> connectivity alert.
    t = clock.advance(hours=7)
    run_once(factory, get_settings(), None, now=t)
    assert any(a.kind == "connectivity" for a in _alerts(factory))
    # Heartbeat resumes -> connectivity resolved.
    w.post(f"/v1/collector-devices/{cid}/heartbeat", {})
    run_once(factory, get_settings(), None, now=t)
    conn = [a for a in _alerts(factory) if a.kind == "connectivity"][0]
    assert conn.status == "resolved" and conn.resolution_reason == "heartbeat_resumed"


def test_no_freshness_alert_for_unsupported_or_denied_metric(world, factory, clock):
    w, cid = world["wearer"], world["collector_id"]
    caps = {"metrics": {"heart_rate": {"supported": True, "permission": "denied"}}}
    w.post(f"/v1/collector-devices/{cid}/heartbeat", {"capabilities": caps})
    t = clock.advance(hours=5)
    w.post(f"/v1/collector-devices/{cid}/heartbeat", {"capabilities": caps})
    run_once(factory, get_settings(), None, now=t)
    assert _alerts(factory) == []


def test_simulation_profile_synthetic_demo(client, factory):
    w = User(client, "demo")
    c = User(client, "demo-carer")
    pid = w.post("/v1/health-profiles", {"display_name": "Demo", "data_mode": "simulation"}).json()[
        "id"
    ]
    w.post(
        "/v1/collector-devices",
        {"health_profile_id": pid, "installation_id": "sim-install-1", "platform": "android"},
    )
    inv = w.post(f"/v1/health-profiles/{pid}/invitations", {}).json()
    g = c.post("/v1/invitations/accept", {"token": inv["token"]}).json()
    w.post(f"/v1/grants/{g['id']}/confirm")
    register_push(c, "sim-carer-01")
    r = _rule(w, pid, "threshold", "heart_rate", "above")
    bad = w.put(
        f"/v1/health-profiles/{pid}/rules/{r['id']}", {"enabled": True, "threshold_value": 120}
    )
    assert bad.json()["detail"] == "simulation_rules_must_be_synthetic_demo"
    w.put(
        f"/v1/health-profiles/{pid}/rules/{r['id']}",
        {
            "enabled": True,
            "threshold_value": 120,
            "is_synthetic_demo": True,
            "window_minutes": 10,
            "min_samples": 5,
            "max_gap_minutes": 3,
            "freshness_minutes": 3,
        },
    )
    rec = hr_window(T0, [140] * 6, "sim")
    rec["data_origin"] = "simulation:familypulse"
    assert (
        w.post(
            f"/v1/health-profiles/{pid}/ingest",
            batch([rec], data_mode="simulation", installation="sim-install-1"),
        ).status_code
        == 200
    )
    fake = FakeTransport()
    run_once(factory, get_settings(), fake, now=T0)
    a = _alerts(factory)[0]
    assert a.is_simulated and a.reason.startswith("[SIMULATION]")
    assert fake.sent[0].title == "FamilyPulse [SIMULATION]"
    d = c.get(f"/v1/health-profiles/{pid}/dashboard").json()
    assert d["data_mode"] == "simulation" and d["banner"].startswith("SIMULATED DATA")
    assert d["metrics"][0]["latest"]["is_simulated"] is True
