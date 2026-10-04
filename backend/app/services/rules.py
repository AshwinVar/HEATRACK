"""Explainable, deterministic rule evaluation.

* Health thresholds are disabled until configured AND reviewed for the wearer (live), or
  explicitly marked synthetic (simulation profiles only).
* Threshold rules need fresh, non-backfill samples covering the whole window with bounded
  gaps and a minimum count; the condition must persist across every sample in the window.
* One alert per episode (partial unique index), hysteresis for recovery, cooldown for
  repeat notifications, optional reminders while unacknowledged.
* Acknowledgement never resolves; only evidence of recovery does.
* Alert + notification outbox rows are written in the same transaction.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    METRIC_CATEGORY,
    Alert,
    CaregiverGrant,
    HealthProfile,
    Measurement,
    NotificationOutbox,
    PushDevice,
    Rule,
)
from app.services.freshness import active_collectors, daily_value_series, metric_capability

MAX_REMINDERS = 3
TREND_FLOORS = {"resting_heart_rate": 3.0, "sleep": 30.0}
METRIC_LABEL = {
    "heart_rate": "Heart rate",
    "resting_heart_rate": "Resting heart rate",
    "steps": "Steps",
    "sleep": "Sleep",
    "hrv_rmssd": "HRV (RMSSD)",
}


def default_rules(hp: HealthProfile, settings: Settings) -> list[Rule]:
    rules = [
        Rule(
            health_profile_id=hp.id,
            kind="threshold",
            metric="heart_rate",
            enabled=False,
            direction="above",
            window_minutes=30,
            min_samples=5,
            max_gap_minutes=15,
            freshness_minutes=15,
            cooldown_minutes=120,
        ),
        Rule(
            health_profile_id=hp.id,
            kind="threshold",
            metric="heart_rate",
            enabled=False,
            direction="below",
            window_minutes=30,
            min_samples=5,
            max_gap_minutes=15,
            freshness_minutes=15,
            cooldown_minutes=120,
        ),
        Rule(
            health_profile_id=hp.id,
            kind="trend",
            metric="resting_heart_rate",
            enabled=False,
            direction="both",
            deviation_threshold=3.0,
            deviation_floor=3.0,
            baseline_min_days=14,
            baseline_lookback_days=28,
            cooldown_minutes=0,
        ),
        Rule(
            health_profile_id=hp.id,
            kind="trend",
            metric="sleep",
            enabled=False,
            direction="below",
            deviation_threshold=3.0,
            deviation_floor=30.0,
            baseline_min_days=14,
            baseline_lookback_days=28,
            cooldown_minutes=0,
        ),
        Rule(
            health_profile_id=hp.id,
            kind="connectivity",
            metric=None,
            enabled=True,
            freshness_minutes=settings.heartbeat_stale_s // 60,
            cooldown_minutes=360,
        ),
    ]
    for metric in ("heart_rate", "resting_heart_rate", "steps", "sleep"):
        rules.append(
            Rule(
                health_profile_id=hp.id,
                kind="data_availability",
                metric=metric,
                enabled=True,
                freshness_minutes=settings.freshness_budget(metric) // 60,
                cooldown_minutes=360,
            )
        )
    return rules


def _fmt(t: datetime) -> str:
    return t.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M UTC")


def _open_alert(db: Session, rule: Rule) -> Alert | None:
    return db.execute(
        select(Alert).where(Alert.rule_id == rule.id, Alert.status != "resolved")
    ).scalar_one_or_none()


def _recipients(db: Session, hp: HealthProfile, alert: Alert) -> list[tuple[Any, PushDevice]]:
    needed = {"alerts"}
    if alert.metric and alert.kind != "connectivity":
        needed.add(METRIC_CATEGORY[alert.metric])
    else:
        needed.add("connectivity")
    grants = db.execute(
        select(CaregiverGrant).where(
            CaregiverGrant.health_profile_id == hp.id, CaregiverGrant.status == "active"
        )
    ).scalars()
    out: list[tuple[Any, PushDevice]] = []
    for g in grants:
        if not needed.issubset(set(g.categories)):
            continue
        devices = db.execute(
            select(PushDevice).where(
                PushDevice.user_id == g.caregiver_user_id, PushDevice.disabled_at.is_(None)
            )
        ).scalars()
        out.extend((g.caregiver_user_id, d) for d in devices)
    return out


def enqueue_notifications(
    db: Session, hp: HealthProfile, alert: Alert, sequence: int, now: datetime
) -> int:
    n = 0
    for user_id, device in _recipients(db, hp, alert):
        db.add(
            NotificationOutbox(
                alert_id=alert.id,
                recipient_user_id=user_id,
                push_device_id=device.id,
                sequence=sequence,
                next_attempt_at=now,
            )
        )
        n += 1
    alert.last_notified_at = now
    return n


def _start_episode(
    db: Session,
    hp: HealthProfile,
    rule: Rule,
    now: datetime,
    *,
    category: str,
    reason: str,
    evidence: dict[str, Any],
    episode_suffix: str | None = None,
) -> Alert:
    prefix = "[SIMULATION] " if hp.data_mode == "simulation" else ""
    alert = Alert(
        health_profile_id=hp.id,
        rule_id=rule.id,
        rule_config_version=rule.config_version,
        kind=rule.kind,
        metric=rule.metric,
        category=category,
        episode_key=f"{rule.kind}:{rule.id}:{episode_suffix or now.isoformat()}",
        status="open",
        reason=prefix + reason,
        evidence=evidence,
        is_simulated=hp.data_mode == "simulation",
        opened_at=now,
        last_evaluated_at=now,
    )
    db.add(alert)
    db.flush()
    # Cooldown: suppress the push (not the alert) if this rule notified recently.
    last_notified = db.execute(
        select(func.max(Alert.last_notified_at)).where(
            Alert.rule_id == rule.id, Alert.id != alert.id
        )
    ).scalar_one_or_none()
    if last_notified and now - last_notified < timedelta(minutes=rule.cooldown_minutes):
        alert.evidence = {**evidence, "notification": "suppressed_by_cooldown"}
    else:
        enqueue_notifications(db, hp, alert, 0, now)
    return alert


def _resolve(alert: Alert, now: datetime, reason: str) -> None:
    alert.status = "resolved"
    alert.resolved_at = now
    alert.resolution_reason = reason
    alert.last_evaluated_at = now


def _maybe_remind(db: Session, hp: HealthProfile, rule: Rule, alert: Alert, now: datetime) -> None:
    alert.last_evaluated_at = now
    if (
        alert.status == "open"
        and rule.reminder_minutes
        and alert.reminders_sent < MAX_REMINDERS
        and alert.last_notified_at
        and now - alert.last_notified_at >= timedelta(minutes=rule.reminder_minutes)
    ):
        alert.reminders_sent += 1
        enqueue_notifications(db, hp, alert, alert.reminders_sent, now)


# ------------------------------------------------------------------------- threshold
def threshold_active(rule: Rule, hp: HealthProfile) -> bool:
    if not rule.enabled or rule.threshold_value is None or rule.direction is None:
        return False
    if hp.data_mode == "simulation":
        return rule.is_synthetic_demo
    return rule.reviewed_at is not None and not rule.is_synthetic_demo


def _eligible_window(
    db: Session, hp: HealthProfile, rule: Rule, now: datetime
) -> tuple[list[Measurement], str | None]:
    start = now - timedelta(minutes=rule.window_minutes)
    q = select(Measurement).where(
        Measurement.health_profile_id == hp.id,
        Measurement.metric == rule.metric,
        Measurement.measured_at > start,
        Measurement.measured_at <= now,
        Measurement.is_backfill.is_(False),
        Measurement.is_simulated.is_(hp.data_mode == "simulation"),
    )
    pref = (hp.preferred_sources or {}).get(rule.metric or "")
    if pref:
        q = q.where(Measurement.data_origin == pref)
    samples = list(db.execute(q.order_by(Measurement.measured_at)).scalars())
    if len(samples) < rule.min_samples:
        return samples, "insufficient_samples"
    if (now - samples[-1].measured_at) > timedelta(minutes=rule.freshness_minutes):
        return samples, "not_fresh"
    gap = timedelta(minutes=rule.max_gap_minutes)
    times = [start] + [s.measured_at for s in samples] + [now]
    if any(b - a > gap for a, b in zip(times, times[1:], strict=False)):
        return samples, "gap_too_large"
    return samples, None


def evaluate_threshold(db: Session, hp: HealthProfile, rule: Rule, now: datetime) -> Alert | None:
    open_alert = _open_alert(db, rule)
    if not threshold_active(rule, hp):
        if open_alert:
            _resolve(open_alert, now, "rule_disabled")
        return None
    samples, ineligible = _eligible_window(db, hp, rule, now)
    if ineligible:
        if open_alert:
            open_alert.last_evaluated_at = now  # unknown: neither confirm nor resolve
        return None
    thr = float(rule.threshold_value)  # type: ignore[arg-type]
    rec = float(rule.recovery_value if rule.recovery_value is not None else thr)
    values = [s.value for s in samples]
    if rule.direction == "above":
        breached, recovered = all(v > thr for v in values), all(v <= rec for v in values)
        word = "above"
    else:
        breached, recovered = all(v < thr for v in values), all(v >= rec for v in values)
        word = "below"
    if open_alert:
        if recovered:
            _resolve(open_alert, now, "recovered")
        else:
            _maybe_remind(db, hp, rule, open_alert, now)
        return None
    if not breached:
        return None
    label = METRIC_LABEL.get(rule.metric or "", rule.metric or "")
    reason = (
        f"{label} was {word} the configured {thr:g} bpm range limit for all {len(values)} "
        f"readings between {_fmt(samples[0].measured_at)} and {_fmt(samples[-1].measured_at)}. "
        "Activity context is unknown. Outside configured range; this is not a diagnosis."
    )
    evidence = {
        "window_minutes": rule.window_minutes,
        "sample_count": len(values),
        "first_sample_at": samples[0].measured_at.isoformat(),
        "last_sample_at": samples[-1].measured_at.isoformat(),
        "threshold": thr,
        "direction": rule.direction,
        "data_origins": sorted({s.data_origin for s in samples}),
        "synthetic_demo_rule": rule.is_synthetic_demo,
    }
    return _start_episode(db, hp, rule, now, category="current", reason=reason, evidence=evidence)


# ------------------------------------------------------------------------- trend
def trend_status(db: Session, hp: HealthProfile, rule: Rule, now: datetime) -> dict[str, Any]:
    tz = ZoneInfo(hp.timezone)
    target = now.astimezone(tz).date() - timedelta(days=1)  # latest complete local day
    start = target - timedelta(days=rule.baseline_lookback_days)
    series = daily_value_series(db, hp, rule.metric or "", start, target)
    prior = {d: v for d, v in series.items() if d < target}  # excludes target day
    status: dict[str, Any] = {
        "rule_id": str(rule.id),
        "metric": rule.metric,
        "target_day": target.isoformat(),
        "valid_prior_days": len(prior),
        "required_days": rule.baseline_min_days,
    }
    if len(prior) < rule.baseline_min_days:
        return {**status, "state": "insufficient_baseline"}
    fresh_target = daily_value_series(
        db, hp, rule.metric or "", target, target, exclude_backfill=True
    )
    if target not in fresh_target:
        return {**status, "state": "no_target_value"}
    vals = list(prior.values())
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals])
    floor = rule.deviation_floor or TREND_FLOORS.get(rule.metric or "", 1.0)
    scale = max(1.4826 * mad, floor)
    value = fresh_target[target]
    deviation = (value - med) / scale
    return {
        **status,
        "state": "evaluated",
        "value": value,
        "baseline_median": med,
        "baseline_mad": mad,
        "scale": scale,
        "robust_deviation": round(deviation, 2),
        "note": "Robust deviation is a heuristic distance from the wearer's own median, "
        "not a probability or medical risk score.",
    }


def evaluate_trend(db: Session, hp: HealthProfile, rule: Rule, now: datetime) -> Alert | None:
    open_alert = _open_alert(db, rule)
    if not rule.enabled:
        if open_alert:
            _resolve(open_alert, now, "rule_disabled")
        return None
    st = trend_status(db, hp, rule, now)
    if st["state"] != "evaluated":
        return None
    dev = st["robust_deviation"]
    t = rule.deviation_threshold
    hit = (
        (rule.direction == "above" and dev >= t)
        or (rule.direction == "below" and dev <= -t)
        or (rule.direction == "both" and abs(dev) >= t)
    )
    if open_alert:
        if open_alert.evidence.get("target_day") == st["target_day"]:
            open_alert.last_evaluated_at = now
            return None
        _resolve(open_alert, now, "superseded_by_new_day" if hit else "recovered")
        db.flush()
    if not hit:
        return None
    label = METRIC_LABEL.get(rule.metric or "", rule.metric or "")
    unit = "min" if rule.metric == "sleep" else "bpm"
    reason = (
        f"{label} on {st['target_day']} ({st['value']:g} {unit}) differed from the median of "
        f"{st['valid_prior_days']} prior days ({st['baseline_median']:g} {unit}); robust "
        f"deviation {dev:+.1f} (configured limit {t:g}). Insight only, not a diagnosis."
    )
    return _start_episode(
        db,
        hp,
        rule,
        now,
        category="insight",
        reason=reason,
        evidence={k: (str(v) if k == "target_day" else v) for k, v in st.items()},
        episode_suffix=st["target_day"],
    )


# ------------------------------------------------------------------------- engineering
def evaluate_connectivity(
    db: Session, hp: HealthProfile, rule: Rule, now: datetime
) -> Alert | None:
    open_alert = _open_alert(db, rule)
    collectors = active_collectors(db, hp.id)
    beats = [c.last_heartbeat_at for c in collectors if c.last_heartbeat_at]
    if not rule.enabled or not collectors:
        if open_alert:
            _resolve(open_alert, now, "rule_disabled" if not rule.enabled else "no_collector")
        return None
    ref = max(beats) if beats else max(c.created_at for c in collectors)
    late = now - ref > timedelta(minutes=rule.freshness_minutes)
    if open_alert:
        if not late:
            _resolve(open_alert, now, "heartbeat_resumed")
        else:
            _maybe_remind(db, hp, rule, open_alert, now)
        return None
    if not late:
        return None
    reason = (
        f"The wearer's phone has not checked in since {_fmt(ref)} (configured limit "
        f"{rule.freshness_minutes} min). The phone may be off, offline or restricting "
        "background work. This says nothing about the wearer's health."
    )
    return _start_episode(
        db,
        hp,
        rule,
        now,
        category="connectivity",
        reason=reason,
        evidence={"last_heartbeat_at": ref.isoformat(), "limit_minutes": rule.freshness_minutes},
    )


def evaluate_data_availability(
    db: Session, hp: HealthProfile, rule: Rule, now: datetime, heartbeat_limit: timedelta
) -> Alert | None:
    open_alert = _open_alert(db, rule)
    collectors = active_collectors(db, hp.id)
    cap, _ = metric_capability(collectors, rule.metric or "")
    beats = [c.last_heartbeat_at for c in collectors if c.last_heartbeat_at]
    heartbeat_ok = bool(beats) and now - max(beats) <= heartbeat_limit
    if not rule.enabled or cap != "ok":
        # Never raise freshness alerts for unsupported / not-permitted metrics.
        if open_alert:
            _resolve(open_alert, now, "metric_not_available")
        return None
    latest = db.execute(
        select(func.max(func.coalesce(Measurement.end_at, Measurement.measured_at))).where(
            Measurement.health_profile_id == hp.id, Measurement.metric == rule.metric
        )
    ).scalar_one_or_none()
    ref = latest or min(c.created_at for c in collectors)
    stale = now - ref > timedelta(minutes=rule.freshness_minutes)
    if open_alert:
        if not stale:
            _resolve(open_alert, now, "data_resumed")
        else:
            _maybe_remind(db, hp, rule, open_alert, now)
        return None
    if not stale or not heartbeat_ok:
        return None  # missing heartbeat is the connectivity rule's job
    label = METRIC_LABEL.get(rule.metric or "", rule.metric or "")
    since = f"since {_fmt(latest)}" if latest else "since the collector was registered"
    reason = (
        f"No new {label.lower()} measurements {since}, although the phone is checking in. "
        "The watch may not be syncing to the vendor app or Health Connect. No new samples "
        "does not prove the watch is not being worn."
    )
    return _start_episode(
        db,
        hp,
        rule,
        now,
        category="connectivity",
        reason=reason,
        evidence={
            "latest_measurement_at": latest.isoformat() if latest else None,
            "limit_minutes": rule.freshness_minutes,
        },
    )


def evaluate_profile(
    db: Session, hp: HealthProfile, settings: Settings, now: datetime
) -> list[Alert]:
    """Evaluate all rules for one profile inside the caller's transaction."""
    created: list[Alert] = []
    if not hp.monitoring_enabled:
        return created
    rules = db.execute(select(Rule).where(Rule.health_profile_id == hp.id)).scalars().all()
    connectivity = next((r for r in rules if r.kind == "connectivity"), None)
    hb_limit = timedelta(
        minutes=connectivity.freshness_minutes if connectivity else settings.heartbeat_stale_s // 60
    )
    for rule in rules:
        if rule.kind == "threshold":
            a = evaluate_threshold(db, hp, rule, now)
        elif rule.kind == "trend":
            a = evaluate_trend(db, hp, rule, now)
        elif rule.kind == "connectivity":
            a = evaluate_connectivity(db, hp, rule, now)
        elif rule.kind == "data_availability":
            a = evaluate_data_availability(db, hp, rule, now, hb_limit)
        else:
            a = None
        if a is not None:
            created.append(a)
    return created
