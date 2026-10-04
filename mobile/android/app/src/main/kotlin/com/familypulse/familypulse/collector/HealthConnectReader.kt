package com.familypulse.familypulse.collector

import android.content.Context
import android.os.Build
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.HealthConnectFeatures
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.HeartRateVariabilityRmssdRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.RestingHeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.response.ChangesResponse
import androidx.health.connect.client.time.TimeRangeFilter
import org.json.JSONObject
import java.time.Instant
import kotlin.reflect.KClass

/** Thin wrapper over Health Connect: status, permissions, paged reads and changes. */
class HealthConnectReader(
    private val context: Context,
) {
    companion object {
        /** HRV stays off until the Fitbit -> Health Connect mapping is verified on hardware. */
        const val ENABLE_HRV = false

        val METRIC_TYPES: Map<String, KClass<out Record>> =
            buildMap {
                put("heart_rate", HeartRateRecord::class)
                put("resting_heart_rate", RestingHeartRateRecord::class)
                put("steps", StepsRecord::class)
                put("sleep", SleepSessionRecord::class)
                if (ENABLE_HRV) put("hrv_rmssd", HeartRateVariabilityRmssdRecord::class)
            }

        val READ_PERMISSIONS: Map<String, String> =
            METRIC_TYPES.mapValues { HealthPermission.getReadPermission(it.value) }

        const val BACKGROUND_PERMISSION = HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND
        const val PROVIDER_PACKAGE = "com.google.android.apps.healthdata"
        private const val PAGE_SIZE = 1000
        private const val MAX_PAGES_PER_TYPE = 50
    }

    fun sdkStatus(): String =
        when (HealthConnectClient.getSdkStatus(context, PROVIDER_PACKAGE)) {
            HealthConnectClient.SDK_AVAILABLE -> "available"
            HealthConnectClient.SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED -> "update_required"
            else -> "not_installed"
        }

    val client: HealthConnectClient by lazy { HealthConnectClient.getOrCreate(context) }

    fun backgroundFeatureAvailable(): Boolean =
        try {
            client.features.getFeatureStatus(HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND) ==
                HealthConnectFeatures.FEATURE_STATUS_AVAILABLE
        } catch (e: Exception) {
            false
        }

    suspend fun grantedPermissions(): Set<String> =
        if (sdkStatus() == "available") client.permissionController.getGrantedPermissions() else emptySet()

    /** Metric keys whose read permission is granted. */
    suspend fun grantedMetrics(granted: Set<String>? = null): Set<String> {
        val g = granted ?: grantedPermissions()
        return READ_PERMISSIONS.filterValues { it in g }.keys
    }

    /** Capability snapshot uploaded to the server (drives per-metric freshness states). */
    suspend fun capabilities(appVersion: String): JSONObject {
        val sdk = sdkStatus()
        val granted =
            try {
                grantedPermissions()
            } catch (e: Exception) {
                emptySet()
            }
        val bgFeature = sdk == "available" && backgroundFeatureAvailable()
        val metrics = JSONObject()
        for (m in listOf("heart_rate", "resting_heart_rate", "steps", "sleep", "hrv_rmssd")) {
            val perm = READ_PERMISSIONS[m]
            metrics.put(
                m,
                JSONObject()
                    .put("supported", sdk == "available" && perm != null)
                    .put("permission", if (perm != null && perm in granted) "granted" else "denied"),
            )
        }
        return JSONObject()
            .put("health_connect", sdk)
            .put(
                "background_read",
                when {
                    !bgFeature -> "unavailable"
                    BACKGROUND_PERMISSION in granted -> "granted"
                    else -> "denied"
                },
            ).put("metrics", metrics)
            .put("hrv_enabled", ENABLE_HRV)
            .put("android_sdk", Build.VERSION.SDK_INT)
            .put("app_version", appVersion)
    }

    suspend fun changesToken(metrics: Set<String>): String =
        client.getChangesToken(ChangesTokenRequest(metrics.mapNotNull { METRIC_TYPES[it] }.toSet()))

    suspend fun changes(token: String): ChangesResponse = client.getChanges(token)

    suspend fun <T : Record> readRange(
        type: KClass<T>,
        start: Instant,
        end: Instant,
    ): List<T> {
        val out = ArrayList<T>()
        var pageToken: String? = null
        var pages = 0
        do {
            val resp =
                client.readRecords(
                    ReadRecordsRequest(
                        recordType = type,
                        timeRangeFilter = TimeRangeFilter.between(start, end),
                        pageSize = PAGE_SIZE,
                        pageToken = pageToken,
                    ),
                )
            out.addAll(resp.records)
            pageToken = resp.pageToken
            pages++
        } while (pageToken != null && pages < MAX_PAGES_PER_TYPE)
        return out
    }

    suspend fun readMetrics(
        metrics: Set<String>,
        start: Instant,
        end: Instant,
    ): List<Record> = metrics.mapNotNull { METRIC_TYPES[it] }.flatMap { readRange(it, start, end) }
}
