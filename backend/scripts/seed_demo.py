"""Seed an explicitly labelled SIMULATION profile through the real HTTP API.

Development only: uses /v1/dev/token (FP_DEV_TOKEN_MINT_ENABLED=true, loopback only).
Every record uses data_origin "simulation:familypulse" and the profile's data_mode is
"simulation", so none of this can appear in, or alert on, a live profile.

Fixtures:
  * unusual HR: 12 synthetic readings above a SYNTHETIC demo threshold (120 bpm)
  * daily trend: 20 baseline days of resting HR + a deviating "yesterday"
  * insufficient baseline: sleep trend enabled with only 5 days of history
  * stale metric: steps last recorded 2 days ago
  * missing heartbeat: no heartbeat is ever sent; connectivity limit set to 5 minutes

Usage:  python -m scripts.seed_demo --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ORIGIN = "simulation:familypulse"
INSTALL = "simulation-collector-01"


class Api:
    def __init__(self, base: str, token: str | None = None) -> None:
        self.base, self.token = base.rstrip("/"), token

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)  # noqa: S310
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}") from e
        return json.loads(raw) if raw else None


def dev_user_id(email: str) -> str:
    """Same deterministic id the app's development sign-in uses."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"familypulse-dev:{email}"))


def login(base: str, email: str) -> Api:
    tok = Api(base).call("POST", "/v1/dev/token", {"email": email, "user_id": dev_user_id(email)})
    return Api(base, tok["access_token"])


def rec(rtype: str, rid: str, start: datetime, samples: list[dict[str, Any]],
        end: datetime | None = None) -> dict[str, Any]:
    r: dict[str, Any] = {
        "record_type": rtype, "source_record_id": rid, "data_origin": ORIGIN,
        "record_version": 1, "start_time": start.isoformat(), "samples": samples,
        "device": {"type": "SIMULATED", "manufacturer": "FamilyPulse", "model": "simulator"},
    }
    if end:
        r["end_time"] = end.isoformat()
    return r


def batch(records: list[dict[str, Any]], mode: str = "incremental") -> dict[str, Any]:
    return {"schema_version": 1, "installation_id": INSTALL, "batch_id": str(uuid.uuid4()),
            "mode": mode, "data_mode": "simulation", "records": records, "deletions": []}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    now = datetime.now(UTC).replace(microsecond=0)
    tz = ZoneInfo("Asia/Kolkata")

    wearer = login(args.base_url, "demo-wearer@example.test")
    carer = login(args.base_url, "demo-caregiver@example.test")
    wearer.call("PATCH", "/v1/me", {"display_name": "Demo wearer", "timezone": "Asia/Kolkata"})
    carer.call("PATCH", "/v1/me", {"display_name": "Demo caregiver", "timezone": "Europe/London"})
    hp = wearer.call("POST", "/v1/health-profiles",
                     {"display_name": "Demo (SIMULATED)", "data_mode": "simulation",
                      "contact_phone": "+910000000000"})
    pid = hp["id"]
    caps = {"health_connect": "simulated", "background_read": "simulated",
            "metrics": {m: {"supported": True, "permission": "granted"}
                        for m in ("heart_rate", "resting_heart_rate", "steps", "sleep")}}
    wearer.call("POST", "/v1/collector-devices", {"health_profile_id": pid,
                "installation_id": INSTALL, "platform": "android", "capabilities": caps})

    inv = wearer.call("POST", f"/v1/health-profiles/{pid}/invitations", {})
    grant = carer.call("POST", "/v1/invitations/accept", {"token": inv["token"]})
    wearer.call("POST", f"/v1/grants/{grant['id']}/confirm")

    # Placeholder token: only meaningful with FP_PUSH_TRANSPORT=fake. A real phone registers
    # its own FCM token from the app.
    carer.call("POST", "/v1/push-devices", {"installation_id": "demo-caregiver-phone",
               "token": "FAKE-DEV-TOKEN-not-a-real-fcm-token", "platform": "ios"})

    rules = {(r["kind"], r["metric"], r["direction"]): r
             for r in wearer.call("GET", f"/v1/health-profiles/{pid}/rules")}
    hr_rule = rules[("threshold", "heart_rate", "above")]
    wearer.call("PUT", f"/v1/health-profiles/{pid}/rules/{hr_rule['id']}", {
        "enabled": True, "threshold_value": 120, "recovery_value": 110,
        "is_synthetic_demo": True, "window_minutes": 20, "min_samples": 8,
        "max_gap_minutes": 5, "freshness_minutes": 10})
    for metric in ("resting_heart_rate", "sleep"):
        r = next(v for k, v in rules.items() if k[0] == "trend" and k[1] == metric)
        wearer.call("PUT", f"/v1/health-profiles/{pid}/rules/{r['id']}", {"enabled": True})
    conn = next(v for k, v in rules.items() if k[0] == "connectivity")
    wearer.call("PUT", f"/v1/health-profiles/{pid}/rules/{conn['id']}", {"freshness_minutes": 5})

    # Unusual HR (synthetic): 12 readings 2 minutes apart ending now.
    start = now - timedelta(minutes=22)
    hr = [{"metric": "heart_rate", "value": 128 + (i % 4), "unit": "bpm",
           "time": (start + timedelta(minutes=2 * i)).isoformat()} for i in range(12)]
    records = [rec("HeartRateRecord", "sim-hr-1", start, hr, end=start + timedelta(minutes=22))]

    # Resting HR: 20 baseline days (backfill) + deviating yesterday (incremental).
    today = now.astimezone(tz).date()
    baseline = []
    for d in range(2, 22):
        t = datetime.combine(today - timedelta(days=d), datetime.min.time(), tz) + timedelta(hours=7)
        baseline.append(rec("RestingHeartRateRecord", f"sim-rhr-{d}", t,
                            [{"metric": "resting_heart_rate", "value": 60 + d % 3, "unit": "bpm",
                              "time": t.isoformat()}]))
    y = datetime.combine(today - timedelta(days=1), datetime.min.time(), tz) + timedelta(hours=7)
    records.append(rec("RestingHeartRateRecord", "sim-rhr-1", y,
                       [{"metric": "resting_heart_rate", "value": 74, "unit": "bpm",
                         "time": y.isoformat()}]))

    # Sleep: only 5 nights -> insufficient baseline.
    for d in range(1, 6):
        wake = datetime.combine(today - timedelta(days=d), datetime.min.time(), tz) + timedelta(
            hours=6)
        records.append(rec("SleepSessionRecord", f"sim-sleep-{d}", wake - timedelta(hours=7),
                           [{"metric": "sleep", "value": 400, "unit": "min",
                             "time": (wake - timedelta(hours=7)).isoformat()}], end=wake))

    # Steps: last interval 2 days ago -> stale.
    s0 = now - timedelta(days=2, hours=3)
    records.append(rec("StepsRecord", "sim-steps-1", s0,
                       [{"metric": "steps", "value": 2400, "unit": "count",
                         "time": s0.isoformat()}], end=s0 + timedelta(hours=1)))

    print("backfill:", wearer.call("POST", f"/v1/health-profiles/{pid}/ingest",
                                   batch(baseline, mode="backfill")))
    print("ingest:", wearer.call("POST", f"/v1/health-profiles/{pid}/ingest", batch(records)))
    print(json.dumps({"health_profile_id": pid, "note": "SIMULATED DATA ONLY",
                      "wearer_token": wearer.token, "caregiver_token": carer.token}, indent=2))


if __name__ == "__main__":
    main()
