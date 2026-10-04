package com.familypulse.familypulse.collector

import android.content.Context
import org.json.JSONObject
import java.io.File

/**
 * Durable FIFO of upload batches, each an AES-GCM encrypted file in app-private storage.
 * A batch is removed ONLY after the server confirmed its transaction committed. Its batch_id
 * is fixed at enqueue time so retries are idempotent server-side.
 *
 * Bounded: at most [MAX_ITEMS] batches / [MAX_BYTES] bytes / [MAX_AGE_MS] age. When a cap is
 * hit the oldest batch is dropped, counted in diagnostics, and a bounded Health Connect
 * reconciliation is scheduled so the dropped period is re-read while still available.
 */
class BatchQueue(
    context: Context,
    private val config: CollectorConfig,
) {
    private val dir = File(context.filesDir, "upload_queue").apply { mkdirs() }

    data class Item(
        val file: File,
        val batch: JSONObject,
        val checkpointAfter: String?,
        val checkpointTypes: Set<String>,
    )

    @Synchronized
    fun enqueue(
        batch: JSONObject,
        checkpointAfter: String?,
        checkpointTypes: Set<String>,
    ) {
        enforceCaps()
        val envelope =
            JSONObject()
                .put("batch", batch)
                .put("checkpoint_after", checkpointAfter)
                .put("checkpoint_types", org.json.JSONArray(checkpointTypes.toList()))
                .put("created_at", System.currentTimeMillis())
        val name = "%015d.bin".format(config.nextQueueSeq())
        CryptoBox.writeText(File(dir, name), envelope.toString())
    }

    @Synchronized
    fun peekOldest(): Item? {
        while (true) {
            val f = files().firstOrNull() ?: return null
            val text = CryptoBox.readText(f)
            if (text == null) {
                f.delete() // undecryptable (key reset) -> cannot be uploaded
                config.incrementCounter("dropped_batches")
                config.needsReconcile = true
                continue
            }
            val env = JSONObject(text)
            val typesArr = env.optJSONArray("checkpoint_types")
            val types = (0 until (typesArr?.length() ?: 0)).map { typesArr!!.getString(it) }.toSet()
            return Item(
                f,
                env.getJSONObject("batch"),
                env.optString("checkpoint_after").ifEmpty { null }.takeIf { it != "null" },
                types,
            )
        }
    }

    @Synchronized
    fun remove(item: Item) {
        item.file.delete()
    }

    @Synchronized
    fun size(): Int = files().size

    @Synchronized
    fun bytes(): Long = files().sumOf { it.length() }

    @Synchronized
    fun clear() {
        files().forEach { it.delete() }
    }

    private fun files(): List<File> = (dir.listFiles { f -> f.name.endsWith(".bin") } ?: emptyArray()).sortedBy { it.name }

    private fun enforceCaps() {
        val now = System.currentTimeMillis()
        var fs = files()
        while (fs.isNotEmpty() && (
                fs.size >= MAX_ITEMS ||
                    fs.sumOf { it.length() } >= MAX_BYTES ||
                    now - fs.first().lastModified() > MAX_AGE_MS
            )
        ) {
            fs.first().delete()
            config.incrementCounter("dropped_batches")
            config.needsReconcile = true
            fs = files()
        }
    }

    companion object {
        const val MAX_ITEMS = 300
        const val MAX_BYTES = 25L * 1024 * 1024
        const val MAX_AGE_MS = 7L * 24 * 3600 * 1000
    }
}
