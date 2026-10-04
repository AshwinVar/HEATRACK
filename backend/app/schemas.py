from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import GRANT_CATEGORIES

Metric = Literal["heart_rate", "resting_heart_rate", "steps", "sleep", "hrv_rmssd"]
RecordType = Literal[
    "HeartRateRecord",
    "RestingHeartRateRecord",
    "StepsRecord",
    "SleepSessionRecord",
    "HeartRateVariabilityRmssdRecord",
]

# record type -> (metric, measurement kind, unit, min, max)
RECORD_SPEC: dict[str, tuple[str, str, str, float, float]] = {
    "HeartRateRecord": ("heart_rate", "sample", "bpm", 20, 250),
    "RestingHeartRateRecord": ("resting_heart_rate", "daily_summary", "bpm", 20, 200),
    "StepsRecord": ("steps", "interval", "count", 0, 100_000),
    "SleepSessionRecord": ("sleep", "session", "min", 0, 24 * 60),
    "HeartRateVariabilityRmssdRecord": ("hrv_rmssd", "sample", "ms", 1, 300),
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _valid_tz(v: str) -> str:
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("unknown IANA timezone") from exc
    return v


def _valid_categories(v: list[str]) -> list[str]:
    bad = set(v) - set(GRANT_CATEGORIES)
    if bad or not v:
        raise ValueError(f"categories must be a non-empty subset of {GRANT_CATEGORIES}")
    return sorted(set(v))


# ---------------------------------------------------------------- accounts & sharing
class MeUpdate(Strict):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = None

    _tz = field_validator("timezone")(lambda v: v if v is None else _valid_tz(v))


class HealthProfileCreate(Strict):
    display_name: str = Field(min_length=1, max_length=120)
    timezone: str = "Asia/Kolkata"
    data_mode: Literal["live", "simulation"] = "live"
    contact_phone: str | None = Field(default=None, pattern=r"^\+?[0-9 ()-]{6,24}$")

    _tz = field_validator("timezone")(_valid_tz)


class HealthProfileUpdate(Strict):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = None
    monitoring_enabled: bool | None = None
    contact_phone: str | None = Field(default=None, pattern=r"^\+?[0-9 ()-]{6,24}$")
    preferred_sources: dict[Metric, str] | None = None

    _tz = field_validator("timezone")(lambda v: v if v is None else _valid_tz(v))


class InvitationCreate(Strict):
    categories: list[str] = Field(default_factory=lambda: list(GRANT_CATEGORIES))
    _c = field_validator("categories")(_valid_categories)


class InvitationAccept(Strict):
    token: str = Field(min_length=20, max_length=200)


class GrantOut(BaseModel):
    id: uuid.UUID
    health_profile_id: uuid.UUID
    caregiver_user_id: uuid.UUID
    caregiver_display_name: str | None = None
    caregiver_email: str | None = None
    wearer_display_name: str | None = None
    status: str
    categories: list[str]
    consent_version: str
    created_at: datetime
    confirmed_at: datetime | None
    revoked_at: datetime | None


# ---------------------------------------------------------------- collector & ingest
class CollectorRegister(Strict):
    health_profile_id: uuid.UUID
    installation_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    platform: Literal["android"]
    capabilities: dict[str, Any] = Field(default_factory=dict)


class SyncRunIn(Strict):
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    status: Literal[
        "success",
        "empty",
        "partial",
        "permission_denied",
        "unavailable",
        "network_error",
        "server_error",
        "token_expired_reconciled",
        "failed",
    ]
    trigger: Literal["foreground", "background", "unknown"] = "unknown"
    metrics_found: dict[Metric, int] = Field(default_factory=dict)
    checkpoint: str | None = Field(default=None, max_length=64)
    error_category: str | None = Field(default=None, max_length=64)


class HeartbeatIn(Strict):
    capabilities: dict[str, Any] | None = None
    sync_run: SyncRunIn | None = None


class SampleIn(Strict):
    metric: Metric
    value: float
    unit: str = Field(max_length=16)
    time: AwareDatetime
    end_time: AwareDatetime | None = None


class DeviceInfo(Strict):
    type: str | None = Field(default=None, max_length=32)
    manufacturer: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=64)


class RecordIn(Strict):
    record_type: RecordType
    source_record_id: str = Field(min_length=1, max_length=128)
    data_origin: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$")
    # Health Connect metadata.lastModifiedTime in epoch ms (monotonic per record).
    record_version: int = Field(ge=0)
    start_time: AwareDatetime
    end_time: AwareDatetime | None = None
    zone_offset_seconds: int | None = Field(default=None, ge=-18 * 3600, le=18 * 3600)
    device: DeviceInfo | None = None
    recording_method: str | None = Field(default=None, max_length=32)
    samples: list[SampleIn] = Field(min_length=1, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> RecordIn:
        metric, kind, unit, lo, hi = RECORD_SPEC[self.record_type]
        if self.end_time is not None and self.end_time < self.start_time:
            raise ValueError("end_time before start_time")
        for s in self.samples:
            if s.metric != metric:
                raise ValueError(f"{self.record_type} only carries {metric}")
            if s.unit != unit:
                raise ValueError(f"{metric} unit must be '{unit}'")
            if not (lo <= s.value <= hi):
                raise ValueError(f"{metric} value out of plausible range")
        if kind in ("interval", "session"):
            if self.end_time is None or len(self.samples) != 1:
                raise ValueError(f"{self.record_type} needs end_time and exactly one sample")
            if (self.end_time - self.start_time).total_seconds() > 24 * 3600:
                raise ValueError("interval longer than 24h")
            if self.end_time == self.start_time:
                raise ValueError("zero-length interval")
        elif kind == "daily_summary" or self.record_type == "HeartRateVariabilityRmssdRecord":
            if len(self.samples) != 1:
                raise ValueError(f"{self.record_type} carries exactly one sample")
        else:  # HR series
            end = self.end_time or self.start_time
            for s in self.samples:
                if not (self.start_time <= s.time <= end):
                    raise ValueError("sample time outside record interval")
        if len(str(self.metadata)) > 40_000:
            raise ValueError("metadata too large")
        return self


class DeletionIn(Strict):
    source_record_id: str = Field(min_length=1, max_length=128)


class IngestBatchIn(Strict):
    schema_version: Literal[1]
    installation_id: str = Field(min_length=8, max_length=64)
    batch_id: uuid.UUID
    # "backfill" = historical reconciliation: stored, never eligible for current alerts.
    mode: Literal["incremental", "backfill"] = "incremental"
    data_mode: Literal["live", "simulation"] = "live"
    records: list[RecordIn] = Field(default_factory=list)
    deletions: list[DeletionIn] = Field(default_factory=list, max_length=5000)
    capabilities: dict[str, Any] | None = None


class IngestResult(BaseModel):
    batch_id: uuid.UUID
    duplicate_batch: bool
    records_inserted: int
    records_updated: int
    records_unchanged: int
    records_deleted: int
    samples_written: int


# ---------------------------------------------------------------- rules / alerts / push
class RuleUpdate(Strict):
    enabled: bool | None = None
    direction: Literal["above", "below", "both"] | None = None
    threshold_value: float | None = None
    recovery_value: float | None = None
    window_minutes: int | None = Field(default=None, ge=5, le=24 * 60)
    min_samples: int | None = Field(default=None, ge=1, le=1000)
    max_gap_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    freshness_minutes: int | None = Field(default=None, ge=1, le=7 * 24 * 60)
    cooldown_minutes: int | None = Field(default=None, ge=0, le=7 * 24 * 60)
    reminder_minutes: int | None = Field(default=None, ge=15, le=7 * 24 * 60)
    baseline_min_days: int | None = Field(default=None, ge=3, le=90)
    baseline_lookback_days: int | None = Field(default=None, ge=7, le=180)
    deviation_threshold: float | None = Field(default=None, gt=0, le=20)
    deviation_floor: float | None = Field(default=None, gt=0)
    # Enabling a health threshold requires an explicit statement that values were reviewed
    # for this wearer (e.g. with her clinician). Demo thresholds must be flagged synthetic.
    reviewed_for_wearer: bool = False
    is_synthetic_demo: bool | None = None


class PushDeviceRegister(Strict):
    installation_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    token: str = Field(min_length=10, max_length=4096)
    platform: Literal["android", "ios", "web"]


class CheckInOut(BaseModel):
    id: uuid.UUID
    checked_in_at: datetime


class DevTokenRequest(Strict):
    user_id: uuid.UUID | None = None
    email: str | None = None
