package com.familypulse.familypulse.collector

import android.content.Context
import org.json.JSONObject
import java.util.UUID

/**
 * Non-secret collector settings and sync checkpoint. (Credentials live in [SessionStore];
 * queued health data in [BatchQueue]; both encrypted.)
 */
class CollectorConfig(
    context: Context,
) {
    private val prefs = context.getSharedPreferences("familypulse_collector", Context.MODE_PRIVATE)

    val installationId: String
        get() =
            prefs.getString(KEY_INSTALL, null) ?: synchronized(this) {
                prefs.getString(KEY_INSTALL, null) ?: ("a-" + UUID.randomUUID().toString().replace("-", "")).also {
                    prefs.edit().putString(KEY_INSTALL, it).apply()
                }
            }

    var apiBaseUrl: String?
        get() = prefs.getString("api_base_url", null)
        set(v) = prefs.edit().putString("api_base_url", v).apply()

    var profileId: String?
        get() = prefs.getString("profile_id", null)
        set(v) = prefs.edit().putString("profile_id", v).apply()

    var deviceId: String?
        get() = prefs.getString("device_id", null)
        set(v) = prefs.edit().putString("device_id", v).apply()

    var enabled: Boolean
        get() = prefs.getBoolean("enabled", false)
        set(v) = prefs.edit().putBoolean("enabled", v).apply()

    /** Health Connect changes token: advanced ONLY after the server acknowledged the data. */
    val changesToken: String? get() = prefs.getString("changes_token", null)
    val changesTokenTypes: Set<String> get() = prefs.getStringSet("changes_token_types", emptySet()) ?: emptySet()

    var needsReconcile: Boolean
        get() = prefs.getBoolean("needs_reconcile", false)
        set(v) = prefs.edit().putBoolean("needs_reconcile", v).apply()

    fun commitCheckpoint(
        token: String,
        types: Set<String>,
    ) {
        prefs
            .edit()
            .putString("changes_token", token)
            .putStringSet("changes_token_types", types)
            .putBoolean("needs_reconcile", false)
            .putLong("checkpoint_committed_at", System.currentTimeMillis())
            .apply()
    }

    fun incrementCounter(name: String) {
        prefs.edit().putLong(name, prefs.getLong(name, 0) + 1).apply()
    }

    fun counter(name: String): Long = prefs.getLong(name, 0)

    var lastSync: JSONObject?
        get() = prefs.getString("last_sync", null)?.let { JSONObject(it) }
        set(v) = prefs.edit().putString("last_sync", v?.toString()).apply()

    fun nextQueueSeq(): Long =
        synchronized(this) {
            val n = prefs.getLong("queue_seq", 0) + 1
            prefs.edit().putLong("queue_seq", n).commit()
            n
        }

    /** Wearer disabled collection or signed out: forget checkpoint and settings. */
    fun clearCollectionState() {
        prefs
            .edit()
            .remove("changes_token")
            .remove("changes_token_types")
            .remove("needs_reconcile")
            .remove("last_sync")
            .remove("profile_id")
            .remove("device_id")
            .putBoolean("enabled", false)
            .apply()
    }

    companion object {
        private const val KEY_INSTALL = "installation_id"
    }
}
