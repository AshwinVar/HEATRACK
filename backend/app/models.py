"""SQLAlchemy models. All instants are timestamptz (UTC). Day summaries are computed on read
in the wearer's IANA timezone, so corrections automatically flow into summaries."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

METRICS = ("heart_rate", "resting_heart_rate", "steps", "sleep", "hrv_rmssd")
GRANT_CATEGORIES = ("heart", "sleep", "activity", "alerts", "connectivity")
METRIC_CATEGORY = {
    "heart_rate": "heart",
    "resting_heart_rate": "heart",
    "hrv_rmssd": "heart",
    "sleep": "sleep",
    "steps": "activity",
}


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)


class Profile(Base):
    """Application profile for an authenticated Supabase user (id == JWT sub)."""

    __tablename__ = "profiles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(120), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class HealthProfile(Base):
    __tablename__ = "health_profiles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    display_name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # "live" profiles accept only real collector data; "simulation" profiles only synthetic.
    # Fixed at creation so simulated data can never silently enter a live profile.
    data_mode: Mapped[str] = mapped_column(String(16), default="live")
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    # metric -> preferred data_origin (e.g. {"steps": "com.fitbit.FitbitMobile"})
    preferred_sources: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    __table_args__ = (
        CheckConstraint("data_mode in ('live','simulation')", name="ck_health_profiles_mode"),
    )


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    health_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("health_profiles.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    categories: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(TS)
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="SET NULL")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class CaregiverGrant(Base):
    __tablename__ = "caregiver_grants"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    health_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("health_profiles.id", ondelete="CASCADE"), index=True
    )
    caregiver_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    invitation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invitations.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), default="pending")
    categories: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    consent_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(TS)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)
    __table_args__ = (
        CheckConstraint("status in ('pending','active','revoked')", name="ck_grants_status"),
        Index(
            "uq_grants_live_pair",
            "health_profile_id",
            "caregiver_user_id",
            unique=True,
            postgresql_where=text("status <> 'revoked'"),
        ),
    )


class CollectorDevice(Base):
    __tablename__ = "collector_devices"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    health_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("health_profiles.id", ondelete="CASCADE"), index=True
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    installation_id: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(16))
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_upload_at: Mapped[datetime | None] = mapped_column(TS)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(TS)
    disabled_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("health_profile_id", "installation_id", name="uq_collector_install"),
    )


class IngestBatch(Base):
    """Idempotency record: replaying the same batch UUID returns the stored result."""

    __tablename__ = "ingest_batches"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    health_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("health_profiles.id", ondelete="CASCADE"), index=True
    )
    collector_device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collector_devices.id", ondelete="CASCADE")
    )
    mode: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class SourceRecord(Base):
    __tablename__ = "source_records"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    health_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("health_profiles.id", ondelete="CASCADE")
    )
    data_origin: Mapped[str] = mapped_column(String(255))
    record_type: Mapped[str] = mapped_column(String(64))
    source_record_id: Mapped[str] = mapped_column(String(128))
    record_version: Mapped[int] = mapped_column(BigInteger)
    start_time: Mapped[datetime] = mapped_column(TS)
    end_time: Mapped[datetime | None] = mapped_column(TS)
    zone_offset_seconds: Mapped[int | None] = mapped_column(Integer)
    device: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    recording_method: Mapped[str | None] = mapped_column(String(32))
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(TS)
    is_backfill: Mapped[bool] = mapped_column(Boolean, default=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    first_received_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)
    __table_args__ = (
        UniqueConstraint(
            "health_profile_id",
            "data_origin",
            "record_type",
            "source_record_id",
            name="uq_source_record",
        ),
        Index("ix_source_records_profile_srcid", "health_profile_id", "source_record_id"),
    )


class Measurement(Base):
    __tablename__ = "measurements"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_records.id", ondelete="CASCADE")
    )
    health_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("health_profiles.id", ondelete="CASCADE")
    )
    metric: Mapped[str] = mapped_column(String(32))
    # sample | interval | session | daily_summary  (never mix interval with daily_summary sums)
    kind: Mapped[str] = mapped_column(String(16))
    sample_index: Mapped[int] = mapped_column(Integer)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16))
    measured_at: Mapped[datetime] = mapped_column(TS)
    end_at: Mapped[datetime | None] = mapped_column(TS)
    received_at: Mapped[datetime] = mapped_column(TS)
    data_origin: Mapped[str] = mapped_column(String(255))
    is_backfill: Mapped[bool] = mapped_column(Boolean, default=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        UniqueConstraint("source_record_id", "metric", "sample_index", name="uq_measurement"),
        Index("ix_measurements_profile_metric_time", "health_profile_id", "metric", "measured_at"),
        # Postgres treats NaN = NaN as true, so compare explicitly.
        CheckConstraint(
            "value <> 'NaN'::float8 AND value <> 'Infinity'::float8 "
            "AND value <> '-Infinity'::float8",
            name="ck_measurements_finite",
        ),
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    collector_device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collector_devices.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(TS)
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(32))
    trigger: Mapped[str] = mapped_column(String(16), default="unknown")
    metrics_found: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)
    checkpoint: Mapped[str | None] = mapped_column(String(64))
    error_category: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class CheckIn(Base):
    """Manual "I'm OK" check-in: a timestamp, never a physiological measurement."""

    __tablename__ = "check_ins"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    health_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("health_profiles.id", ondelete="CASCADE"), index=True
    )
    checked_in_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Rule(Base):
    __tablename__ = "rules"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    health_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("health_profiles.id", ondelete="CASCADE"), index=True
    )
    # threshold | trend | connectivity | data_availability
    kind: Mapped[str] = mapped_column(String(24))
    metric: Mapped[str | None] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    direction: Mapped[str | None] = mapped_column(String(8))  # above | below | both
    threshold_value: Mapped[float | None] = mapped_column(Float)
    recovery_value: Mapped[float | None] = mapped_column(Float)  # hysteresis
    window_minutes: Mapped[int] = mapped_column(Integer, default=30)
    min_samples: Mapped[int] = mapped_column(Integer, default=5)
    max_gap_minutes: Mapped[int] = mapped_column(Integer, default=15)
    freshness_minutes: Mapped[int] = mapped_column(Integer, default=60)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=120)
    reminder_minutes: Mapped[int | None] = mapped_column(Integer)
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    baseline_min_days: Mapped[int] = mapped_column(Integer, default=14)
    baseline_lookback_days: Mapped[int] = mapped_column(Integer, default=28)
    deviation_threshold: Mapped[float] = mapped_column(Float, default=3.0)
    deviation_floor: Mapped[float | None] = mapped_column(Float)
    is_synthetic_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    health_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("health_profiles.id", ondelete="CASCADE"), index=True
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("rules.id", ondelete="SET NULL"))
    rule_config_version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24))
    metric: Mapped[str | None] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(16))  # current | insight | connectivity
    episode_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="open")
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    opened_at: Mapped[datetime] = mapped_column(TS)
    last_evaluated_at: Mapped[datetime] = mapped_column(TS)
    last_notified_at: Mapped[datetime | None] = mapped_column(TS)
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0)
    acknowledged_at: Mapped[datetime | None] = mapped_column(TS)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    resolution_reason: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("health_profile_id", "episode_key", name="uq_alert_episode"),
        CheckConstraint("status in ('open','acknowledged','resolved')", name="ck_alerts_status"),
        # At most one unresolved episode per rule.
        Index(
            "uq_alert_open_per_rule",
            "rule_id",
            unique=True,
            postgresql_where=text("status <> 'resolved'"),
        ),
    )


class PushDevice(Base):
    __tablename__ = "push_devices"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    installation_id: Mapped[str] = mapped_column(String(64))
    token: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(String(16))
    last_registered_at: Mapped[datetime] = mapped_column(TS)
    disabled_at: Mapped[datetime | None] = mapped_column(TS)
    disabled_reason: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("user_id", "installation_id", name="uq_push_install"),)


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    alert_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"))
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE")
    )
    push_device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("push_devices.id", ondelete="CASCADE")
    )
    # 0 = initial notification for the episode, n>0 = nth reminder.
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(TS)
    leased_until: Mapped[datetime | None] = mapped_column(TS)
    # Provider (FCM) accepted the message. NOT proof of delivery or of human acknowledgement.
    accepted_at: Mapped[datetime | None] = mapped_column(TS)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    discarded_at: Mapped[datetime | None] = mapped_column(TS)
    discard_reason: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "alert_id", "recipient_user_id", "push_device_id", "sequence", name="uq_outbox_job"
        ),
        Index(
            "ix_outbox_due",
            "next_attempt_at",
            postgresql_where=text("accepted_at IS NULL AND discarded_at IS NULL"),
        ),
    )


class AuditEvent(Base):
    """Minimal audit trail. Never stores raw health values, tokens or secrets."""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64))
    health_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    occurred_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
