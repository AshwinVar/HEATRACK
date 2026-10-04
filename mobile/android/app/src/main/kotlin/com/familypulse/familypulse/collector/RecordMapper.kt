package com.familypulse.familypulse.collector

import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.HeartRateVariabilityRmssdRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.RestingHeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.metadata.Device
import androidx.health.connect.client.records.metadata.Metadata
import org.json.JSONArray
import org.json.JSONObject
import java.time.Duration
import java.time.Instant

/**
 * Maps Health Connect records to the backend ingest schema (schema_version 1), preserving
 * source identity (record id, data origin package, device, lastModifiedTime as version).
 * Values outside the server's plausibility bounds are filtered here so a single odd sample
 * cannot cause the whole batch to be rejected; the count is reported in diagnostics.
 */
object RecordMapper {
    data class Mapped(
        val json: JSONObject,
        val sampleCount: Int,
        val endTime: Instant,
        val metric: String,
    )

    var filteredSamples = 0L
        private set

    private val AWAKE_STAGES =
        setOf(
            SleepSessionRecord.STAGE_TYPE_AWAKE,
            SleepSessionRecord.STAGE_TYPE_AWAKE_IN_BED,
            SleepSessionRecord.STAGE_TYPE_OUT_OF_BED,
        )

    fun map(record: Record): Mapped? =
        when (record) {
            is HeartRateRecord -> {
                heartRate(record)
            }

            is RestingHeartRateRecord -> {
                single(
                    record.metadata,
                    "RestingHeartRateRecord",
                    "resting_heart_rate",
                    "bpm",
                    record.beatsPerMinute.toDouble(),
                    record.time,
                    null,
                    record.zoneOffset?.totalSeconds,
                    20.0,
                    200.0,
                )
            }

            is StepsRecord -> {
                single(
                    record.metadata,
                    "StepsRecord",
                    "steps",
                    "count",
                    record.count.toDouble(),
                    record.startTime,
                    record.endTime,
                    record.startZoneOffset?.totalSeconds,
                    0.0,
                    100_000.0,
                )
            }

            is SleepSessionRecord -> {
                sleep(record)
            }

            is HeartRateVariabilityRmssdRecord -> {
                single(
                    record.metadata,
                    "HeartRateVariabilityRmssdRecord",
                    "hrv_rmssd",
                    "ms",
                    record.heartRateVariabilityMillis,
                    record.time,
                    null,
                    record.zoneOffset?.totalSeconds,
                    1.0,
                    300.0,
                )
            }

            else -> {
                null
            }
        }

    private fun base(
        md: Metadata,
        type: String,
        start: Instant,
        end: Instant?,
        zone: Int?,
    ): JSONObject {
        val j =
            JSONObject()
                .put("record_type", type)
                .put("source_record_id", md.id)
                .put("data_origin", md.dataOrigin.packageName)
                .put("record_version", md.lastModifiedTime.toEpochMilli())
                .put("start_time", start.toString())
                .put("recording_method", recordingMethod(md.recordingMethod))
        end?.let { j.put("end_time", it.toString()) }
        zone?.let { j.put("zone_offset_seconds", it) }
        md.device?.let { j.put("device", device(it)) }
        return j
    }

    private fun heartRate(r: HeartRateRecord): Mapped? {
        val samples = JSONArray()
        var n = 0
        for (s in r.samples.sortedBy { it.time }) {
            val bpm = s.beatsPerMinute.toDouble()
            if (bpm < 20 || bpm > 250) {
                filteredSamples++
                continue
            }
            samples.put(sample("heart_rate", bpm, "bpm", s.time))
            n++
            if (n >= 2000) break
        }
        if (n == 0) return null
        val j = base(r.metadata, "HeartRateRecord", r.startTime, r.endTime, r.startZoneOffset?.totalSeconds)
        j.put("samples", samples)
        return Mapped(j, n, r.endTime, "heart_rate")
    }

    private fun sleep(r: SleepSessionRecord): Mapped? {
        val total = Duration.between(r.startTime, r.endTime)
        if (total.isNegative || total.isZero || total > Duration.ofHours(24)) {
            filteredSamples++
            return null
        }
        val stages = r.stages.sortedBy { it.startTime }
        // Minutes asleep: exclude awake / out-of-bed stages when stages exist.
        val asleep =
            if (stages.isEmpty()) {
                total
            } else {
                stages
                    .filter { it.stage !in AWAKE_STAGES }
                    .fold(Duration.ZERO) { acc, st -> acc + Duration.between(st.startTime, st.endTime) }
            }
        val j = base(r.metadata, "SleepSessionRecord", r.startTime, r.endTime, r.startZoneOffset?.totalSeconds)
        j.put("samples", JSONArray().put(sample("sleep", asleep.toMinutes().toDouble(), "min", r.startTime)))
        val stageArr = JSONArray()
        stages.take(150).forEach {
            stageArr.put(
                JSONObject()
                    .put("stage", it.stage)
                    .put("start", it.startTime.toString())
                    .put("end", it.endTime.toString()),
            )
        }
        j.put("metadata", JSONObject().put("stages", stageArr).put("stages_present", stages.isNotEmpty()))
        return Mapped(j, 1, r.endTime, "sleep")
    }

    private fun single(
        md: Metadata,
        type: String,
        metric: String,
        unit: String,
        value: Double,
        start: Instant,
        end: Instant?,
        zone: Int?,
        lo: Double,
        hi: Double,
    ): Mapped? {
        if (value.isNaN() || value.isInfinite() || value < lo || value > hi) {
            filteredSamples++
            return null
        }
        if (end != null && (!end.isAfter(start) || Duration.between(start, end) > Duration.ofHours(24))) {
            filteredSamples++
            return null
        }
        val j = base(md, type, start, end, zone)
        j.put("samples", JSONArray().put(sample(metric, value, unit, start)))
        return Mapped(j, 1, end ?: start, metric)
    }

    private fun sample(
        metric: String,
        value: Double,
        unit: String,
        t: Instant,
    ) = JSONObject()
        .put("metric", metric)
        .put("value", value)
        .put("unit", unit)
        .put("time", t.toString())

    private fun device(d: Device): JSONObject =
        JSONObject()
            .put(
                "type",
                when (d.type) {
                    Device.TYPE_WATCH -> "WATCH"
                    Device.TYPE_PHONE -> "PHONE"
                    else -> "TYPE_${d.type}"
                },
            ).apply {
                d.manufacturer?.let { put("manufacturer", it.take(64)) }
                d.model?.let { put("model", it.take(64)) }
            }

    private fun recordingMethod(m: Int): String =
        when (m) {
            Metadata.RECORDING_METHOD_ACTIVELY_RECORDED -> "active"
            Metadata.RECORDING_METHOD_AUTOMATICALLY_RECORDED -> "automatic"
            Metadata.RECORDING_METHOD_MANUAL_ENTRY -> "manual"
            else -> "unknown"
        }
}
