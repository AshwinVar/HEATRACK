from __future__ import annotations

from datetime import timedelta

from tests.conftest import T0, batch, daily_record, hr_record


def _metrics(user, pid):
    d = user.get(f"/v1/health-profiles/{pid}/dashboard").json()
    return {m["metric"]: m for m in d["metrics"]}, d


def test_waiting_for_data_before_any_measurement(world):
    m, d = _metrics(world["carer"], world["pid"])
    assert m["heart_rate"]["state"] == "waiting_for_data"
    assert m["hrv_rmssd"]["state"] == "unsupported"
    assert d["summary_text"] == "No current data available"
    assert "oxygen_saturation" in d["unavailable_metrics"]


def test_heartbeat_and_empty_upload_never_refresh_metric_freshness(world, clock):
    w, c, pid = world["wearer"], world["carer"], world["pid"]
    w.post(
        f"/v1/health-profiles/{pid}/ingest",
        batch([hr_record("hr-1", T0 - timedelta(minutes=5), [70, 71])]),
    )
    assert _metrics(c, pid)[0]["heart_rate"]["state"] == "fresh"
    clock.advance(hours=4)  # beyond the 3h HR budget
    w.post(f"/v1/collector-devices/{world['collector_id']}/heartbeat", {})
    r = w.post(f"/v1/health-profiles/{pid}/ingest", batch([]))
    assert r.status_code == 200
    m, d = _metrics(c, pid)
    hr = m["heart_rate"]
    assert hr["state"] == "stale"
    assert hr["latest"]["value"] == 71  # old value is shown with its age, never as current
    assert hr["age_seconds"] >= 4 * 3600 - 60
    dev = d["collector"]["devices"][0]
    assert dev["heartbeat_state"] == "ok" and dev["last_upload_at"] is not None
    assert d["summary_text"] == "No current data available"  # never "healthy"


def test_stale_sleep_alongside_fresh_hr(world):
    w, c, pid = world["wearer"], world["carer"], world["pid"]
    old_sleep = daily_record(
        "SleepSessionRecord",
        "sl-1",
        T0 - timedelta(days=3, hours=8),
        420,
        end=T0 - timedelta(days=3, hours=1),
    )
    w.post(
        f"/v1/health-profiles/{pid}/ingest",
        batch([hr_record("hr-1", T0 - timedelta(minutes=5), [70]), old_sleep]),
    )
    m, d = _metrics(c, pid)
    assert m["heart_rate"]["state"] == "fresh"
    assert m["sleep"]["state"] == "stale"
    assert m["steps"]["state"] == "waiting_for_data"
    assert d["summary_text"] == "No configured anomalies detected in available data"


def test_permission_denied_state(world):
    w, c, pid = world["wearer"], world["carer"], world["pid"]
    caps = {
        "metrics": {
            "heart_rate": {"supported": True, "permission": "denied"},
            "sleep": {"supported": False, "permission": "denied"},
        }
    }
    w.post(f"/v1/collector-devices/{world['collector_id']}/heartbeat", {"capabilities": caps})
    m, _ = _metrics(c, pid)
    assert m["heart_rate"]["state"] == "permission_denied"
    assert m["sleep"]["state"] == "unsupported"


def test_latest_shows_source_and_times(world, clock):
    w, c, pid = world["wearer"], world["carer"], world["pid"]
    w.post(
        f"/v1/health-profiles/{pid}/ingest",
        batch([hr_record("hr-1", T0 - timedelta(minutes=20), [70, 75], step_minutes=10)]),
    )
    clock.advance(minutes=5)
    hr = _metrics(c, pid)[0]["heart_rate"]
    assert hr["latest"]["data_origin"] == "com.fitbit.FitbitMobile"
    assert hr["latest"]["device"]["model"] == "Fitbit"
    assert hr["latest"]["measured_at"].startswith("2026-10-04T05:50")
    assert hr["latest"]["received_at"].startswith("2026-10-04T06:00")
    assert hr["age_seconds"] == 15 * 60


def test_daily_summary_uses_wearer_timezone(world):
    w, c, pid = world["wearer"], world["carer"], world["pid"]
    # 19:00 UTC on 3 Oct = 00:30 IST on 4 Oct -> counts toward 4 Oct in Asia/Kolkata.
    rec = daily_record(
        "StepsRecord",
        "st",
        T0.replace(day=3, hour=19),
        300,
        end=T0.replace(day=3, hour=19, minute=20),
    )
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([rec]))
    daily = c.get(f"/v1/health-profiles/{pid}/dashboard").json()["daily"]
    assert daily[-1]["date"] == "2026-10-04" and daily[-1]["timezone"] == "Asia/Kolkata"
    assert daily[-1]["steps"]["selected"]["value"] == 300
    assert daily[-2]["steps"]["by_origin"] == {}
