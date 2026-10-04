package com.familypulse.familypulse.collector

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.IOException

/**
 * The single owner of the Supabase session on Android.
 *
 * Supabase rotates refresh tokens and revokes the whole session if a rotated token is
 * reused, so two independent refreshers (Flutter UI + background worker) would eventually
 * sign the wearer out. On Android, Flutter therefore never refreshes itself: it asks this
 * store for an access token over the MethodChannel, and both the UI and WorkManager share
 * one process-wide mutex. Credentials are AES-GCM encrypted with an Android Keystore key.
 */
class SessionStore(
    context: Context,
) {
    private val file = File(context.filesDir, "secure/session.bin")

    data class Session(
        val accessToken: String,
        val refreshToken: String?,
        val expiresAtEpochSec: Long,
        val mode: String, // "supabase" | "dev"
        val supabaseUrl: String?,
        val anonKey: String?,
    ) {
        fun toJson(): JSONObject =
            JSONObject()
                .put("access_token", accessToken)
                .put("refresh_token", refreshToken)
                .put("expires_at", expiresAtEpochSec)
                .put("mode", mode)
                .put("supabase_url", supabaseUrl)
                .put("anon_key", anonKey)

        companion object {
            fun fromJson(j: JSONObject) =
                Session(
                    accessToken = j.getString("access_token"),
                    refreshToken = j.optString("refresh_token").ifEmpty { null },
                    expiresAtEpochSec = j.getLong("expires_at"),
                    mode = j.optString("mode", "supabase"),
                    supabaseUrl = j.optString("supabase_url").ifEmpty { null },
                    anonKey = j.optString("anon_key").ifEmpty { null },
                )
        }
    }

    class AuthRequired : Exception("auth_required")

    fun save(s: Session?) {
        if (s == null) file.delete() else CryptoBox.writeText(file, s.toJson().toString())
    }

    fun load(): Session? =
        CryptoBox.readText(file)?.let {
            try {
                Session.fromJson(JSONObject(it))
            } catch (e: Exception) {
                null
            }
        }

    /**
     * Returns a valid access token, refreshing if it expires within 2 minutes.
     * @throws AuthRequired if the wearer must sign in again.
     * @throws IOException on network failure (caller retries later).
     */
    suspend fun accessToken(): String =
        mutex.withLock {
            val s = load() ?: throw AuthRequired()
            val now = System.currentTimeMillis() / 1000
            if (s.expiresAtEpochSec - now > 120) return@withLock s.accessToken
            if (s.mode != "supabase" || s.refreshToken == null || s.supabaseUrl == null || s.anonKey == null) {
                throw AuthRequired()
            }
            val refreshed = refresh(s)
            save(refreshed)
            refreshed.accessToken
        }

    private suspend fun refresh(s: Session): Session =
        withContext(Dispatchers.IO) {
            val url = s.supabaseUrl!!.trimEnd('/') + "/auth/v1/token?grant_type=refresh_token"
            val res =
                Http.postJson(
                    url,
                    JSONObject().put("refresh_token", s.refreshToken),
                    bearer = null,
                    extraHeaders = mapOf("apikey" to s.anonKey!!),
                )
            when {
                res.code == 200 -> {
                    val j = JSONObject(res.body)
                    val expiresAt =
                        if (j.has("expires_at")) {
                            j.getLong("expires_at")
                        } else {
                            System.currentTimeMillis() / 1000 + j.optLong("expires_in", 3600)
                        }
                    s.copy(
                        accessToken = j.getString("access_token"),
                        refreshToken = j.optString("refresh_token").ifEmpty { s.refreshToken },
                        expiresAtEpochSec = expiresAt,
                    )
                }

                res.code in 400..499 -> {
                    save(null)
                    throw AuthRequired()
                }

                else -> {
                    throw IOException("refresh failed: ${res.code}")
                }
            }
        }

    companion object {
        /** Process-wide: the UI channel and WorkManager run in the same process. */
        private val mutex = Mutex()
    }
}
