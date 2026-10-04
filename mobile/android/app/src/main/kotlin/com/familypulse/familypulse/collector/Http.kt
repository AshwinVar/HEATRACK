package com.familypulse.familypulse.collector

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

object Http {
    data class Response(
        val code: Int,
        val body: String,
    )

    private val DEV_HOSTS = setOf("10.0.2.2", "localhost", "127.0.0.1")

    /** HTTPS only, except explicit local development hosts (debug network config). */
    fun checkUrl(url: String) {
        val u = URL(url)
        if (u.protocol != "https" && u.host !in DEV_HOSTS) {
            throw IOException("refusing non-HTTPS URL")
        }
    }

    fun postJson(
        url: String,
        body: JSONObject?,
        bearer: String?,
        extraHeaders: Map<String, String> = emptyMap(),
    ): Response = request("POST", url, body, bearer, extraHeaders)

    fun request(
        method: String,
        url: String,
        body: JSONObject?,
        bearer: String?,
        extraHeaders: Map<String, String> = emptyMap(),
    ): Response {
        checkUrl(url)
        val conn = URL(url).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = method
            conn.connectTimeout = 15_000
            conn.readTimeout = 30_000
            conn.setRequestProperty("Accept", "application/json")
            bearer?.let { conn.setRequestProperty("Authorization", "Bearer $it") }
            extraHeaders.forEach { (k, v) -> conn.setRequestProperty(k, v) }
            if (body != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
            return Response(code, text)
        } finally {
            conn.disconnect()
        }
    }
}
