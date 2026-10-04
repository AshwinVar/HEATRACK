"""Transactional batch ingestion with two layers of idempotency:

1. batch UUID  -> replaying the same batch returns the stored result without re-processing;
2. source record identity (profile, origin, type, Health Connect record id) + record_version
   -> resending the same records under a *different* batch UUID writes nothing new.

Newer record versions replace their samples atomically (corrections); deletions remove
samples and mark the source record deleted. Summaries are computed on read, so they follow.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth import AuthUser
from app.authz import require_owner
from app.config import Settings
from app.models import CollectorDevice, IngestBatch, Measurement, SourceRecord
from app.schemas import RECORD_SPEC, IngestBatchIn, IngestResult, RecordIn
from app.services.audit import audit

SIMULATION_ORIGIN_PREFIX = "simulation:"


def _reject(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _check_limits_and_times(batch: IngestBatchIn, settings: Settings, now: datetime) -> None:
    if len(batch.records) > settings.ingest_max_records:
        raise _reject("too_many_records")
    total = 0
    latest_ok = now + timedelta(seconds=settings.ingest_max_future_skew_seconds)
    earliest_ok = now - timedelta(days=settings.ingest_max_age_days)
    for r in batch.records:
        if len(r.samples) > settings.ingest_max_samples_per_record:
            raise _reject("too_many_samples_in_record")
        total += len(r.samples)
        times = [r.start_time] + ([r.end_time] if r.end_time else [])
        times += [s.time for s in r.samples] + [s.end_time for s in r.samples if s.end_time]
        for t in times:
            if t > latest_ok:
                raise _reject("timestamp_in_future")
            if t < earliest_ok:
                raise _reject("timestamp_too_old")
        simulated_origin = r.data_origin.startswith(SIMULATION_ORIGIN_PREFIX)
        if simulated_origin != (batch.data_mode == "simulation"):
            raise _reject("simulation_origin_mismatch")
    if total > settings.ingest_max_total_samples:
        raise _reject("too_many_samples")


def _measurements_for(sr: SourceRecord, rec: RecordIn, received_at: datetime) -> list[Measurement]:
    metric, kind, unit, _, _ = RECORD_SPEC[rec.record_type]
    ordered = sorted(rec.samples, key=lambda s: (s.time, s.value))
    out = []
    for idx, s in enumerate(ordered):
        end_at = s.end_time
        if kind in ("interval", "session"):
            end_at = rec.end_time
        out.append(
            Measurement(
                source_record_id=sr.id,
                health_profile_id=sr.health_profile_id,
                metric=metric,
                kind=kind,
                sample_index=idx,
                value=float(s.value),
                unit=unit,
                measured_at=rec.start_time if kind in ("interval", "session") else s.time,
                end_at=end_at,
                received_at=received_at,
                data_origin=rec.data_origin,
                is_backfill=sr.is_backfill,
                is_simulated=sr.is_simulated,
                source_metadata={
                    k: v
                    for k, v in {
                        "device": rec.device.model_dump(exclude_none=True) if rec.device else None,
                        "recording_method": rec.recording_method,
                        "zone_offset_seconds": rec.zone_offset_seconds,
                        **({"stages": rec.metadata.get("stages")} if metric == "sleep" else {}),
                    }.items()
                    if v is not None
                },
            )
        )
    return out


def ingest_batch(
    db: Session,
    user: AuthUser,
    profile_id: object,
    batch: IngestBatchIn,
    settings: Settings,
    now: datetime,
) -> IngestResult:
    access = require_owner(db, user, profile_id)  # type: ignore[arg-type]
    profile = access.profile

    # Bind to a registered, enabled collector installation of this owner, and serialise
    # concurrent uploads from it (row lock) so dedup decisions cannot race.
    device = db.execute(
        select(CollectorDevice)
        .where(
            CollectorDevice.health_profile_id == profile.id,
            CollectorDevice.installation_id == batch.installation_id,
            CollectorDevice.owner_user_id == user.id,
            CollectorDevice.disabled_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="collector_not_registered")

    if batch.data_mode != profile.data_mode:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="data_mode_mismatch")

    existing_batch = db.get(IngestBatch, batch.batch_id)
    if existing_batch is not None:
        if existing_batch.health_profile_id != profile.id:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="batch_id_conflict")
        return IngestResult(**{**existing_batch.result, "duplicate_batch": True})

    _check_limits_and_times(batch, settings, now)

    counts = dict(inserted=0, updated=0, unchanged=0, deleted=0, samples=0)
    is_backfill = batch.mode == "backfill"
    is_sim = batch.data_mode == "simulation"

    for rec in batch.records:
        sr = db.execute(
            select(SourceRecord).where(
                SourceRecord.health_profile_id == profile.id,
                SourceRecord.data_origin == rec.data_origin,
                SourceRecord.record_type == rec.record_type,
                SourceRecord.source_record_id == rec.source_record_id,
            )
        ).scalar_one_or_none()
        if sr is not None and rec.record_version <= sr.record_version:
            counts["unchanged"] += 1
            continue
        if sr is None:
            sr = SourceRecord(
                health_profile_id=profile.id,
                data_origin=rec.data_origin,
                record_type=rec.record_type,
                source_record_id=rec.source_record_id,
                first_received_at=now,
                is_backfill=is_backfill,
                is_simulated=is_sim,
            )
            db.add(sr)
            counts["inserted"] += 1
        else:
            db.execute(delete(Measurement).where(Measurement.source_record_id == sr.id))
            counts["updated"] += 1
            # A correction delivered by a backfill must not make old data alert-eligible,
            # and an incremental correction keeps the original backfill flag.
            sr.is_backfill = sr.is_backfill or is_backfill
        sr.record_version = rec.record_version
        sr.start_time = rec.start_time
        sr.end_time = rec.end_time
        sr.zone_offset_seconds = rec.zone_offset_seconds
        sr.device = rec.device.model_dump(exclude_none=True) if rec.device else {}
        sr.recording_method = rec.recording_method
        sr.deleted = False
        sr.deleted_at = None
        sr.updated_at = now
        db.flush()
        ms = _measurements_for(sr, rec, now)
        db.add_all(ms)
        counts["samples"] += len(ms)
        db.flush()

    for d in batch.deletions:
        rows = db.execute(
            select(SourceRecord).where(
                SourceRecord.health_profile_id == profile.id,
                SourceRecord.source_record_id == d.source_record_id,
                SourceRecord.deleted.is_(False),
            )
        ).scalars()
        for sr in rows:
            db.execute(delete(Measurement).where(Measurement.source_record_id == sr.id))
            sr.deleted = True
            sr.deleted_at = now
            sr.updated_at = now
            counts["deleted"] += 1

    # Upload time is tracked separately from measurement time; an empty upload never
    # refreshes metric freshness because freshness is derived only from measurements.
    device.last_upload_at = now
    if batch.capabilities is not None:
        device.capabilities = batch.capabilities

    result = IngestResult(
        batch_id=batch.batch_id,
        duplicate_batch=False,
        records_inserted=counts["inserted"],
        records_updated=counts["updated"],
        records_unchanged=counts["unchanged"],
        records_deleted=counts["deleted"],
        samples_written=counts["samples"],
    )
    db.add(
        IngestBatch(
            id=batch.batch_id,
            health_profile_id=profile.id,
            collector_device_id=device.id,
            mode=batch.mode,
            result=result.model_dump(mode="json"),
        )
    )
    audit(
        db,
        user.id,
        "ingest",
        profile.id,
        batch_id=str(batch.batch_id),
        records=len(batch.records),
        deletions=len(batch.deletions),
    )
    db.commit()  # respond only after commit
    return result
