package com.familypulse.familypulse.collector

import android.content.Context
import android.util.Log
import androidx.health.connect.client.changes.DeletionChange
import androidx.health.connect.client.changes.UpsertionChange
import androidx.health.connect.client.records.Record
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.time.Duration
import java.time.Instant
import java.util.UUID

/**
 * One sync cycle, shared by "Sync now" (foreground) and the WorkManager worker (background):
 *
 * 1. Flush the durable queue first (oldest first; stop on the first failure).
 * 2. Only when the queue is empty, read new data from Health Connect:
 *    - first run / token expired / permission set changed: get a NEW changes token *before*
 *      a bounded paged reconciliation read of the last [RECONCILE_DAYS] days;
 *    - otherwise: page through getChanges (upserts + deletions).
 *    Batches are queued with the next token attached to the last one; the checkpoint is
 *    committed only when that batch is acknowledged by the server.
 * 3. Heartbeat (collector liveness + capability snapshot + sync summary). The heartbeat
 *    never affects metric freshness server-side.
 *
 * Background reads require READ_HEALTH_DATA_IN_BACKGROUND; without it, background runs only
 * flush/heartbeat and the state is reported as "foreground_only".
 */
class SyncEngine(
    private val context: Context,
) {
    private val config = CollectorConfig(context)
    private val queue = BatchQueue(context, config)
    private val session = SessionStore(context)
    private val reader = HealthConnectReader(context)

    enum class Outcome { OK, RETRY, AUTH_REQUIRED, NOT_CONFIGURED, FATAL }

    data class Summary(
        val outcome: Outcome,
        val json: JSONObject,
    )

    private sealed class Flush {
        object Done : Flush()

        object Retry : Flush()

        object Auth : Flush()

        data class Fatal(
            val reason: String,
        ) : Flush()
    }

    suspend fun run(trigger: String): Summary =
        runMutex.withLock {
            withContext(Dispatchers.IO) { runLocked(trigger) }
        }

    private suspend fun runLocked(trigger: String): Summary {
        val started = Instant.now()
        val pid = config.profileId
        val base = config.apiBaseUrl
        if (!config.enabled || pid == null || base == null) {
            return Summary(Outcome.NOT_CONFIGURED, JSONObject().put("status", "not_configured"))
        }
        val found = mutableMapOf<String, Int>()
        var status = "success"
        var errorCategory: String? = null
        val caps =
            try {
                reader.capabilities(appVersion())
            } catch (e: Exception) {
                JSONObject().put("health_connect", "error")
            }
        val bgGranted = caps.optString("background_read") == "granted"
        val hcAvailable = caps.optString("health_connect") == "available"
        val readAllowed = hcAvailable && (trigger == "foreground" || bgGranted)

        var flush = flushQueue(base, pid)
        if (flush == Flush.Done && readAllowed) {
            try {
                collect(caps, found)
                flush = flushQueue(base, pid)
            } catch (e: SecurityException) {
                status = "permission_denied"
                errorCategory = "hc_security_exception"
            } catch (e: IOException) {
                status = "failed"
                errorCategory = "hc_io"
            } catch (e: Exception) {
                // Health Connect rate limiting / remote exceptions surface here.
                Log.w(TAG, "health connect read failed: ${e.javaClass.simpleName}")
                status = "failed"
                errorCategory = "hc_" + e.javaClass.simpleName.take(40)
            }
        } else if (!hcAvailable) {
            status = "unavailable"
            errorCategory = "health_connect_" + caps.optString("health_connect")
        } else if (!readAllowed) {
            status = "partial"
            errorCategory = "foreground_only"
        }
        val outcome =
            when (flush) {
                Flush.Done -> {
                    Outcome.OK
                }

                Flush.Retry -> {
                    status = "network_error"
                    errorCategory = errorCategory ?: "upload_retry"
                    Outcome.RETRY
                }

                Flush.Auth -> {
                    status = "failed"
                    errorCategory = "auth_required"
                    Outcome.AUTH_REQUIRED
                }

                is Flush.Fatal -> {
                    status = "failed"
                    errorCategory = flush.reason
                    Outcome.FATAL
                }
            }
        if (status == "success" && found.values.sum() == 0) status = "empty"

        val ended = Instant.now()
        val summary =
            JSONObject()
                .put("trigger", trigger)
                .put("started_at", started.toString())
                .put("ended_at", ended.toString())
                .put("status", status)
                .put("error_category", errorCategory)
                .put("metrics_found", JSONObject(found as Map<*, *>))
                .put("queue_size", queue.size())
                .put("checkpoint", if (config.changesToken != null) "set" else "none")
                .put("background_read", caps.optString("background_read"))
        if (outcome != Outcome.AUTH_REQUIRED) {
            heartbeat(base, caps, summary)
        }
        config.lastSync = summary
        return Summary(outcome, summary)
    }

    // --------------------------------------------------------------------------- upload
    private suspend fun flushQueue(
        base: String,
        pid: String,
    ): Flush {
        while (true) {
            val item = queue.peekOldest() ?: return Flush.Done
            val token =
                try {
                    session.accessToken()
                } catch (e: SessionStore.AuthRequired) {
                    return Flush.Auth
                } catch (e: IOException) {
                    return Flush.Retry
                }
            val res =
                try {
                    Http.postJson("$base/v1/health-profiles/$pid/ingest", item.batch, token)
                } catch (e: IOException) {
                    return Flush.Retry
                }
            when {
                res.code == 200 -> {
                    queue.remove(item) // only after the server's committed acknowledgement
                    item.checkpointAfter?.let { config.commitCheckpoint(it, item.checkpointTypes) }
                }

                res.code == 401 -> {
                    return Flush.Auth
                }

                res.code == 422 -> {
                    // Server rejected the content; retrying cannot help. Drop and count it.
                    Log.w(TAG, "batch rejected by server (422)")
                    queue.remove(item)
                    config.incrementCounter("rejected_batches")
                    item.checkpointAfter?.let { config.commitCheckpoint(it, item.checkpointTypes) }
                }

                res.code in listOf(403, 404, 409) -> {
                    return Flush.Fatal("server_${res.code}")
                }

                else -> {
                    return Flush.Retry
                } // 5xx, 429, etc.
            }
        }
    }

    private suspend fun heartbeat(
        base: String,
        caps: JSONObject,
        summary: JSONObject,
    ) {
        val deviceId = config.deviceId ?: return
        try {
            val token = session.accessToken()
            val run =
                JSONObject()
                    .put("started_at", summary.getString("started_at"))
                    .put("ended_at", summary.getString("ended_at"))
                    .put("status", summary.getString("status"))
                    .put("trigger", summary.getString("trigger"))
                    .put("metrics_found", summary.getJSONObject("metrics_found"))
                    .put("checkpoint", summary.getString("checkpoint"))
            summary
                .optString("error_category")
                .takeIf { it.isNotEmpty() && it != "null" }
                ?.let { run.put("error_category", it.take(64)) }
            Http.postJson(
                "$base/v1/collector-devices/$deviceId/heartbeat",
                JSONObject().put("capabilities", caps).put("sync_run", run),
                token,
            )
        } catch (e: Exception) {
            Log.w(TAG, "heartbeat failed: ${e.javaClass.simpleName}")
        }
    }

    // --------------------------------------------------------------------------- collect
    private suspend fun collect(
        caps: JSONObject,
        found: MutableMap<String, Int>,
    ) {
        val granted = reader.grantedMetrics()
        if (granted.isEmpty()) throw SecurityException("no read permissions")
        val token = config.changesToken
        if (token == null || config.needsReconcile || config.changesTokenTypes != granted) {
            reconcile(granted, caps, found)
            return
        }
        val upserts = ArrayList<Record>()
        val deletions = ArrayList<String>()
        var next = token
        var pages = 0
        while (true) {
            val resp = reader.changes(next)
            if (resp.changesTokenExpired) {
                // Tokens expire (~30 days unused). Bounded, paged reconciliation instead.
                reconcile(granted, caps, found)
                return
            }
            for (ch in resp.changes) {
                when (ch) {
                    is UpsertionChange -> upserts.add(ch.record)
                    is DeletionChange -> deletions.add(ch.recordId)
                }
            }
            next = resp.nextChangesToken
            pages++
            if (!resp.hasMore || pages >= MAX_CHANGE_PAGES) break
        }
        enqueue(upserts, deletions, next, granted, caps, found, reconciling = false)
    }

    private suspend fun reconcile(
        granted: Set<String>,
        caps: JSONObject,
        found: MutableMap<String, Int>,
    ) {
        // Token first, so anything written during the read is captured by later changes.
        val newToken = reader.changesToken(granted)
        val end = Instant.now()
        val records = reader.readMetrics(granted, end.minus(Duration.ofDays(RECONCILE_DAYS)), end)
        enqueue(records, emptyList(), newToken, granted, caps, found, reconciling = true)
    }

    /**
     * Splits records into backfill (older than [LIVE_WINDOW]) and incremental batches. The
     * server never raises current alerts from backfill. The changes token is attached to the
     * final batch so the checkpoint advances only after everything before it is acknowledged.
     */
    private fun enqueue(
        records: List<Record>,
        deletions: List<String>,
        nextToken: String,
        types: Set<String>,
        caps: JSONObject,
        found: MutableMap<String, Int>,
        reconciling: Boolean,
    ) {
        val liveCutoff = Instant.now().minus(LIVE_WINDOW)
        val mapped = records.mapNotNull { RecordMapper.map(it) }
        mapped.forEach { found[it.metric] = (found[it.metric] ?: 0) + it.sampleCount }
        // Deduplicate within this read (same record id may appear more than once in changes).
        val latest = mapped.associateBy { it.json.getString("source_record_id") + "|" + it.json.getString("data_origin") }.values
        val (old, recent) = latest.partition { reconciling && it.endTime.isBefore(liveCutoff) }
        val batches = ArrayList<JSONObject>()
        batches += chunk(old.toList(), emptyList(), "backfill", caps)
        batches += chunk(recent.toList(), deletions, "incremental", caps)
        if (batches.isEmpty()) {
            config.commitCheckpoint(nextToken, types) // nothing to acknowledge
            return
        }
        batches.forEachIndexed { i, b ->
            val last = i == batches.lastIndex
            queue.enqueue(b, if (last) nextToken else null, if (last) types else emptySet())
        }
    }

    private fun chunk(
        items: List<RecordMapper.Mapped>,
        deletions: List<String>,
        mode: String,
        caps: JSONObject,
    ): List<JSONObject> {
        fun batch(
            records: JSONArray,
            dels: List<String>,
        ): JSONObject {
            val delArr = JSONArray()
            dels.forEach { delArr.put(JSONObject().put("source_record_id", it)) }
            return JSONObject()
                .put("schema_version", 1)
                .put("installation_id", config.installationId)
                .put("batch_id", UUID.randomUUID().toString()) // fixed for all retries
                .put("mode", mode)
                .put("data_mode", "live")
                .put("records", records)
                .put("deletions", delArr)
                .put("capabilities", caps)
        }
        val out = ArrayList<JSONObject>()
        var cur = JSONArray()
        var samples = 0
        for (m in items.sortedBy { it.endTime }) {
            if (cur.length() >= MAX_RECORDS_PER_BATCH || samples + m.sampleCount > MAX_SAMPLES_PER_BATCH) {
                out += batch(cur, emptyList())
                cur = JSONArray()
                samples = 0
            }
            cur.put(m.json)
            samples += m.sampleCount
        }
        if (cur.length() > 0) out += batch(cur, emptyList())
        // Deletions go after upserts so an upsert+delete of the same record ends deleted.
        deletions.distinct().chunked(MAX_DELETIONS_PER_BATCH).forEach { out += batch(JSONArray(), it) }
        return out
    }

    // --------------------------------------------------------------------------- diagnostics

    /**
     * Integration spike: what does Health Connect actually contain on THIS phone?
     * Reports per-metric record counts, data origins, sample cadence and export lag
     * (lastModifiedTime - newest sample time). Nothing is uploaded.
     */
    suspend fun diagnose(hours: Long): JSONObject =
        withContext(Dispatchers.IO) {
            val out = JSONObject().put("health_connect", reader.sdkStatus())
            if (reader.sdkStatus() != "available") return@withContext out
            val granted = reader.grantedPermissions()
            out.put("background_feature", reader.backgroundFeatureAvailable())
            out.put("background_permission", HealthConnectReader.BACKGROUND_PERMISSION in granted)
            val end = Instant.now()
            val metrics = JSONObject()
            for ((metric, type) in HealthConnectReader.METRIC_TYPES) {
                val perm = HealthConnectReader.READ_PERMISSIONS.getValue(metric)
                val m = JSONObject().put("permission", if (perm in granted) "granted" else "denied")
                if (perm in granted) {
                    val window = if (metric == "heart_rate") Duration.ofHours(hours) else Duration.ofDays(7)
                    try {
                        val recs = reader.readRange(type, end.minus(window), end)
                        m.put("window_hours", window.toHours()).put("records", recs.size)
                        val origins = JSONObject()
                        recs
                            .groupingBy { it.metadata.dataOrigin.packageName }
                            .eachCount()
                            .forEach { (k, v) -> origins.put(k, v) }
                        m.put("origins", origins)
                        val mapped = recs.mapNotNull { RecordMapper.map(it) }
                        if (mapped.isNotEmpty()) {
                            val newest = mapped.maxBy { it.endTime }
                            m.put("newest_measurement", newest.endTime.toString())
                            m.put("newest_age_minutes", Duration.between(newest.endTime, end).toMinutes())
                        }
                        val lags =
                            recs
                                .mapNotNull { r ->
                                    RecordMapper.map(r)?.let { Duration.between(it.endTime, r.metadata.lastModifiedTime).toMinutes() }
                                }.sorted()
                        if (lags.isNotEmpty()) m.put("median_export_lag_minutes", lags[lags.size / 2])
                        if (metric == "heart_rate") {
                            val times =
                                recs
                                    .filterIsInstance<androidx.health.connect.client.records.HeartRateRecord>()
                                    .flatMap { r -> r.samples.map { it.time } }
                                    .sorted()
                            m.put("samples", times.size)
                            if (times.size > 1) {
                                val gaps = times.zipWithNext { a, b -> Duration.between(a, b).seconds }.sorted()
                                m.put("median_sample_gap_seconds", gaps[gaps.size / 2])
                                m.put("max_sample_gap_minutes", gaps.last() / 60)
                            }
                        }
                    } catch (e: Exception) {
                        m.put("error", e.javaClass.simpleName)
                    }
                }
                metrics.put(metric, m)
            }
            out.put("metrics", metrics)
            out.put("filtered_out_of_range_samples", RecordMapper.filteredSamples)
            out
        }

    fun status(): JSONObject =
        JSONObject()
            .put("enabled", config.enabled)
            .put("installation_id", config.installationId)
            .put("profile_id", config.profileId)
            .put("device_id", config.deviceId)
            .put("api_base_url", config.apiBaseUrl)
            .put("queue_size", queue.size())
            .put("queue_bytes", queue.bytes())
            .put("dropped_batches", config.counter("dropped_batches"))
            .put("rejected_batches", config.counter("rejected_batches"))
            .put("checkpoint", if (config.changesToken != null) "set" else "none")
            .put("needs_reconcile", config.needsReconcile)
            .put("last_sync", config.lastSync)
            .put("signed_in", session.load() != null)

    /** Disable collection / sign out: cancel work, delete queued data and checkpoint. */
    fun disableAndClear(clearSession: Boolean) {
        SyncWorker.cancelAll(context)
        queue.clear()
        config.clearCollectionState()
        if (clearSession) session.save(null)
    }

    private fun appVersion(): String =
        try {
            context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "?"
        } catch (e: Exception) {
            "?"
        }

    companion object {
        private const val TAG = "FamilyPulseSync"
        private val runMutex = Mutex()
        const val RECONCILE_DAYS = 7L
        val LIVE_WINDOW: Duration = Duration.ofHours(2)
        const val MAX_CHANGE_PAGES = 20
        const val MAX_RECORDS_PER_BATCH = 200
        const val MAX_SAMPLES_PER_BATCH = 10_000
        const val MAX_DELETIONS_PER_BATCH = 2_000
    }
}
