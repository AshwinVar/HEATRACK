"""Per-metric freshness and wearer-local daily summaries.

Freshness is derived *only* from measurement timestamps. Heartbeats and successful (possibly
empty) uploads never make a metric look current. An old normal value is reported as stale,
never as current.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import METRICS, CollectorDevice, HealthProfile, Measurement

# HRV is stored if uploaded but is not requested by the collector until the Fitbit ->
# Health Connect mapping has been verified on real hardware.
DEFAULT_COLLECTED = ("heart_rate", "resting_heart_rate", "steps", "sleep")

UNAVAILABLE_METRICS = {
    "oxygen_saturation": "Google Health lists SpO2 as readable but not written to Health "
    "Connect; not shown unless an authorized source is independently verified.",
    "ecg_afib": "Out of scope: no verified source.",
    "falls": "Out of scope: no verified source.",
    "blood_pressure": "Out of scope: no verified source.",
    "glucose": "Out of scope: no verified source.",
    "watch_battery": "Not available through Health Connect.",
}


@dataclass
class MetricStatus:
    metric: str
    state: str  # unsupported | permission_denied | waiting_for_data | fresh | stale
    reason: str | None
    latest: dict[str, Any] | None
    freshness_budget_seconds: int
    age_seconds: int | None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def active_collectors(db: Session, profile_id: Any) -> list[CollectorDevice]:
    return list(
        db.execute(
            select(CollectorDevice).where(
                CollectorDevice.health_profile_id == profile_id,
                CollectorDevice.disabled_at.is_(None),
            )
        ).scalars()
    )


def metric_capability(collectors: list[CollectorDevice], metric: str) -> tuple[str, str | None]:
    """Combine capability snapshots: 'ok', 'unsupported', 'permission_denied', 'unknown'."""
    if not collectors:
        return "unknown", "No collector device registered yet."
    best = "unsupported"
    reason: str | None = "Collector reports this metric is not supported."
    for c in collectors:
        m = (c.capabilities or {}).get("metrics", {}).get(metric)
        if not m:
            continue
        if m.get("supported") and m.get("permission") == "granted":
            return "ok", None
        if m.get("supported") and best == "unsupported":
            best, reason = "permission_denied", "Health Connect read permission not granted."
    if best == "unsupported" and metric not in DEFAULT_COLLECTED:
        reason = "Not collected: mapping not yet verified on real hardware."
    return best, reason


def _latest(db: Session, hp: HealthProfile, metric: str) -> Measurement | None:
    q = select(Measurement).where(
        Measurement.health_profile_id == hp.id, Measurement.metric == metric
    )
    origin = (hp.preferred_sources or {}).get(metric)
    if origin:
        q = q.where(Measurement.data_origin == origin)
    q = q.order_by(func.coalesce(Measurement.end_at, Measurement.measured_at).desc()).limit(1)
    return db.execute(q).scalar_one_or_none()


def measurement_out(m: Measurement) -> dict[str, Any]:
    return {
        "value": m.value,
        "unit": m.unit,
        "kind": m.kind,
        "measured_at": m.measured_at,
        "end_at": m.end_at,
        "received_at": m.received_at,
        "data_origin": m.data_origin,
        "device": (m.source_metadata or {}).get("device"),
        "is_simulated": m.is_simulated,
        "is_backfill": m.is_backfill,
    }


def metric_status(
    db: Session,
    hp: HealthProfile,
    metric: str,
    settings: Settings,
    now: datetime,
    collectors: list[CollectorDevice] | None = None,
) -> MetricStatus:
    collectors = active_collectors(db, hp.id) if collectors is None else collectors
    budget = settings.freshness_budget(metric)
    cap, reason = metric_capability(collectors, metric)
    latest = _latest(db, hp, metric)
    age = None
    latest_out = None
    if latest is not None:
        t = latest.end_at or latest.measured_at
        age = max(0, int((now - t).total_seconds()))
        latest_out = measurement_out(latest)
    if cap == "unsupported":
        state = "unsupported"
    elif cap == "permission_denied":
        state = "permission_denied"
    elif latest is None:
        state = "waiting_for_data"
        reason = reason or "Supported and permitted, but no measurements received yet."
    elif age is not None and age <= budget:
        state, reason = "fresh", None
    else:
        state = "stale"
        reason = "Latest measurement is older than the freshness budget."
    return MetricStatus(metric, state, reason, latest_out, budget, age)


def local_day(t: datetime, tz: ZoneInfo) -> date:
    return t.astimezone(tz).date()


def daily_summaries(
    db: Session, hp: HealthProfile, now: datetime, days: int = 7, metrics: set[str] | None = None
) -> list[dict[str, Any]]:
    """Per wearer-local day. Steps/sleep are reported per data origin; a single 'selected'
    total is given only when one origin exists or a preferred origin is configured, so phone
    and Fitbit steps are never summed together, and interval records are never mixed with
    daily aggregates."""
    tz = ZoneInfo(hp.timezone)
    today = now.astimezone(tz).date()
    first = today - timedelta(days=days - 1)
    start_utc = datetime.combine(first, datetime.min.time(), tz) - timedelta(days=1)
    wanted = metrics or {"steps", "sleep", "resting_heart_rate"}
    rows = db.execute(
        select(Measurement).where(
            Measurement.health_profile_id == hp.id,
            Measurement.metric.in_(wanted & {"steps", "sleep", "resting_heart_rate"}),
            Measurement.measured_at >= start_utc,
        )
    ).scalars()
    steps: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    sleep: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    rhr: dict[date, dict[str, tuple[datetime, float]]] = defaultdict(dict)
    for m in rows:
        if m.metric == "steps" and m.kind == "interval":
            steps[local_day(m.measured_at, tz)][m.data_origin] += m.value
        elif m.metric == "sleep" and m.kind == "session":
            # Sleep is attributed to the local day the wearer woke up.
            sleep[local_day(m.end_at or m.measured_at, tz)][m.data_origin] += m.value
        elif m.metric == "resting_heart_rate":
            d = local_day(m.measured_at, tz)
            prev = rhr[d].get(m.data_origin)
            if prev is None or m.measured_at > prev[0]:
                rhr[d][m.data_origin] = (m.measured_at, m.value)
    pref = hp.preferred_sources or {}

    def select_total(by_origin: dict[str, float], metric: str) -> dict[str, Any]:
        origin = pref.get(metric)
        if origin and origin in by_origin:
            return {"value": by_origin[origin], "origin": origin}
        if len(by_origin) == 1:
            ((o, v),) = by_origin.items()
            return {"value": v, "origin": o}
        return {"value": None, "origin": None}  # ambiguous: show per origin

    out = []
    for i in range(days):
        d = first + timedelta(days=i)
        entry: dict[str, Any] = {"date": d.isoformat(), "timezone": hp.timezone}
        if "steps" in wanted:
            entry["steps"] = {
                "by_origin": dict(steps.get(d, {})),
                "selected": select_total(dict(steps.get(d, {})), "steps"),
                "complete_day": d < today,
            }
        if "sleep" in wanted:
            entry["sleep_minutes"] = {
                "by_origin": dict(sleep.get(d, {})),
                "selected": select_total(dict(sleep.get(d, {})), "sleep"),
            }
        if "resting_heart_rate" in wanted:
            by = {o: v for o, (_, v) in rhr.get(d, {}).items()}
            entry["resting_heart_rate"] = {
                "by_origin": by,
                "selected": select_total(by, "resting_heart_rate"),
            }
        out.append(entry)
    return out


def daily_value_series(
    db: Session,
    hp: HealthProfile,
    metric: str,
    start_day: date,
    end_day: date,
    *,
    exclude_backfill: bool = False,
) -> dict[date, float]:
    """Single value per local day (selected origin) for trend rules."""
    tz = ZoneInfo(hp.timezone)
    start_utc = datetime.combine(start_day, datetime.min.time(), tz) - timedelta(days=1)
    end_utc = datetime.combine(end_day + timedelta(days=2), datetime.min.time(), tz)
    q = select(Measurement).where(
        Measurement.health_profile_id == hp.id,
        Measurement.metric == metric,
        Measurement.measured_at >= start_utc,
        Measurement.measured_at < end_utc,
    )
    if exclude_backfill:
        q = q.where(Measurement.is_backfill.is_(False))
    pref = (hp.preferred_sources or {}).get(metric)
    if pref:
        q = q.where(Measurement.data_origin == pref)
    per_day: dict[date, dict[str, list[tuple[datetime, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for m in db.execute(q).scalars():
        t = m.end_at if metric == "sleep" and m.end_at else m.measured_at
        d = local_day(t, tz)
        if start_day <= d <= end_day:
            per_day[d][m.data_origin].append((m.measured_at, m.value))
    out: dict[date, float] = {}
    for d, by_origin in per_day.items():
        if len(by_origin) != 1:
            continue  # ambiguous multi-source day without a preferred origin: not valid
        (vals,) = by_origin.values()
        if metric == "sleep":
            out[d] = sum(v for _, v in vals)
        else:
            out[d] = max(vals)[1]  # latest reading of the day
    return out


def all_metric_statuses(
    db: Session, hp: HealthProfile, settings: Settings, now: datetime, allowed: set[str]
) -> list[dict[str, Any]]:
    collectors = active_collectors(db, hp.id)
    return [
        metric_status(db, hp, m, settings, now, collectors).as_dict()
        for m in METRICS
        if m in allowed
    ]
