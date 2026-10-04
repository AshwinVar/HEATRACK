from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select

from app.models import Measurement, SourceRecord
from tests.conftest import T0, batch, daily_record, hr_record


def _count(factory, model=Measurement):
    with factory() as db:
        return db.scalar(select(func.count()).select_from(model))


def test_repeated_batch_is_idempotent(world, factory):
    w, pid = world["wearer"], world["pid"]
    b = batch([hr_record("hr-1", T0 - timedelta(minutes=10), [70, 72, 74])])
    r1 = w.post(f"/v1/health-profiles/{pid}/ingest", b).json()
    r2 = w.post(f"/v1/health-profiles/{pid}/ingest", b).json()
    assert r1["records_inserted"] == 1 and r1["samples_written"] == 3
    assert r2["duplicate_batch"] is True and r2["samples_written"] == 3
    assert _count(factory) == 3


def test_duplicate_source_records_under_new_batch_id(world, factory):
    """Offline retry that re-reads Health Connect and generates a new batch UUID."""
    w, pid = world["wearer"], world["pid"]
    rec = hr_record("hr-1", T0 - timedelta(minutes=10), [70, 72, 74])
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([rec]))
    r = w.post(f"/v1/health-profiles/{pid}/ingest", batch([rec, rec])).json()
    assert r["duplicate_batch"] is False
    assert r["records_unchanged"] == 2 and r["samples_written"] == 0
    assert _count(factory) == 3 and _count(factory, SourceRecord) == 1


def test_updated_record_replaces_samples_atomically(world, factory):
    w, pid = world["wearer"], world["pid"]
    start = T0 - timedelta(minutes=10)
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_record("hr-1", start, [70, 72, 74])]))
    r = w.post(
        f"/v1/health-profiles/{pid}/ingest", batch([hr_record("hr-1", start, [80, 81], version=2)])
    ).json()
    assert r["records_updated"] == 1
    with factory() as db:
        vals = sorted(db.scalars(select(Measurement.value)).all())
    assert vals == [80, 81]
    # An older version arriving late is ignored.
    r = w.post(
        f"/v1/health-profiles/{pid}/ingest", batch([hr_record("hr-1", start, [60], version=1)])
    ).json()
    assert r["records_unchanged"] == 1 and _count(factory) == 2


def test_deleted_record_removes_samples_and_updates_summary(world, clock):
    w, pid = world["wearer"], world["pid"]
    s1 = daily_record(
        "StepsRecord", "st-1", T0 - timedelta(hours=2), 1000, end=T0 - timedelta(hours=1)
    )
    s2 = daily_record(
        "StepsRecord", "st-2", T0 - timedelta(hours=1), 500, end=T0 - timedelta(minutes=30)
    )
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([s1, s2]))
    today = w.get(f"/v1/health-profiles/{pid}/dashboard").json()["daily"][-1]
    assert today["steps"]["selected"]["value"] == 1500
    r = w.post(f"/v1/health-profiles/{pid}/ingest", batch([], deletions=["st-1"])).json()
    assert r["records_deleted"] == 1
    today = w.get(f"/v1/health-profiles/{pid}/dashboard").json()["daily"][-1]
    assert today["steps"]["selected"]["value"] == 500


def test_phone_and_fitbit_steps_not_summed(world):
    w, pid = world["wearer"], world["pid"]
    a = daily_record(
        "StepsRecord", "fb", T0 - timedelta(hours=2), 1000, end=T0 - timedelta(hours=1)
    )
    b = daily_record(
        "StepsRecord",
        "ph",
        T0 - timedelta(hours=2),
        900,
        end=T0 - timedelta(hours=1),
        origin="com.google.android.apps.fitness",
    )
    w.post(f"/v1/health-profiles/{pid}/ingest", batch([a, b]))
    steps = w.get(f"/v1/health-profiles/{pid}/dashboard").json()["daily"][-1]["steps"]
    assert steps["by_origin"] == {
        "com.fitbit.FitbitMobile": 1000,
        "com.google.android.apps.fitness": 900,
    }
    assert steps["selected"]["value"] is None  # ambiguous: never 1900
    w.client.patch(
        f"/v1/health-profiles/{pid}",
        headers=w.h,
        json={"preferred_sources": {"steps": "com.fitbit.FitbitMobile"}},
    )
    steps = w.get(f"/v1/health-profiles/{pid}/dashboard").json()["daily"][-1]["steps"]
    assert steps["selected"] == {"value": 1000, "origin": "com.fitbit.FitbitMobile"}


def test_rejects_bad_units_nan_future_and_ranges(world, factory):
    w, pid = world["wearer"], world["pid"]
    url = f"/v1/health-profiles/{pid}/ingest"
    rec = hr_record("hr-1", T0 - timedelta(minutes=5), [70])
    bad_unit = json.loads(json.dumps(rec))
    bad_unit["samples"][0]["unit"] = "bps"
    assert w.post(url, batch([bad_unit])).status_code == 422
    out_of_range = hr_record("hr-2", T0 - timedelta(minutes=5), [400])
    assert w.post(url, batch([out_of_range])).status_code == 422
    future = hr_record("hr-3", T0 + timedelta(hours=2), [70])
    assert w.post(url, batch([future])).status_code == 422
    ancient = hr_record("hr-4", T0 - timedelta(days=800), [70])
    assert w.post(url, batch([ancient])).status_code == 422
    naive = json.loads(json.dumps(rec))
    naive["samples"][0]["time"] = "2026-10-04T05:55:00"
    assert w.post(url, batch([naive])).status_code == 422
    # NaN must be rejected even though Python's JSON parser accepts the literal.
    raw = json.dumps(batch([rec])).replace('"value": 70', '"value": NaN')
    r = w.client.post(url, content=raw, headers={**w.h, "Content-Type": "application/json"})
    assert r.status_code == 422
    raw = json.dumps(batch([rec])).replace('"value": 70', '"value": Infinity')
    r = w.client.post(url, content=raw, headers={**w.h, "Content-Type": "application/json"})
    assert r.status_code == 422
    # Steps interval without end time / extra fields rejected.
    steps = daily_record("StepsRecord", "s", T0 - timedelta(hours=1), 10)
    assert w.post(url, batch([steps])).status_code == 422
    extra = {**rec, "user_id": "someone-else"}
    assert w.post(url, batch([extra])).status_code == 422
    assert _count(factory) == 0


def test_batch_size_limits(world):
    w, pid = world["wearer"], world["pid"]
    recs = [hr_record(f"r{i}", T0 - timedelta(minutes=30), [70]) for i in range(501)]
    assert w.post(f"/v1/health-profiles/{pid}/ingest", batch(recs)).status_code == 422


def test_simulated_data_never_enters_live_profile(world, factory):
    w, pid = world["wearer"], world["pid"]
    url = f"/v1/health-profiles/{pid}/ingest"
    sim = hr_record("s1", T0 - timedelta(minutes=5), [70], origin="simulation:familypulse")
    assert w.post(url, batch([sim], data_mode="simulation")).status_code == 409
    assert w.post(url, batch([sim])).status_code == 422  # sim origin in a live batch
    assert _count(factory) == 0


def test_measurements_pagination(world):
    w, pid = world["wearer"], world["pid"]
    w.post(
        f"/v1/health-profiles/{pid}/ingest",
        batch([hr_record("hr-1", T0 - timedelta(minutes=30), list(range(60, 85)))]),
    )
    url = (
        f"/v1/health-profiles/{pid}/measurements?metric=heart_rate&limit=10"
        "&from=2026-10-04T00:00:00Z&to=2026-10-04T07:00:00Z"
    )
    got, cursor = [], None
    while True:
        r = w.get(url + (f"&cursor={cursor}" if cursor else "")).json()
        got += [p["value"] for p in r["points"]]
        cursor = r["next_cursor"]
        if not cursor:
            break
    assert got == list(range(60, 85))
    p = r["points"][0]
    assert p["origin"] == "com.fitbit.FitbitMobile" and p["received_at"]


def test_concurrent_retries_do_not_duplicate(world, factory):
    """An offline retry racing the original upload (same records, different batch ids)."""
    from concurrent.futures import ThreadPoolExecutor

    w, pid = world["wearer"], world["pid"]
    rec = hr_record("hr-race", T0 - timedelta(minutes=10), [70, 72, 74, 76])
    same_batch = batch([rec])
    bodies = [same_batch, same_batch] + [batch([rec]) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(
            ex.map(lambda b: w.post(f"/v1/health-profiles/{pid}/ingest", b).status_code, bodies)
        )
    assert codes == [200] * 8
    assert _count(factory) == 4 and _count(factory, SourceRecord) == 1
