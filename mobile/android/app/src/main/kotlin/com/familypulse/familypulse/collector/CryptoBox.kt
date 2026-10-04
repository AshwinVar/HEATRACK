package com.familypulse.familypulse.collector

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * AES-256-GCM with a non-exportable Android Keystore key. Used for everything sensitive the
 * collector writes to disk: queued health batches and auth credentials. Files live in
 * app-private storage and are excluded from backup (see data_extraction_rules.xml).
 */
object CryptoBox {
    private const val ALIAS = "familypulse_local_v1"
    private const val TRANSFORM = "AES/GCM/NoPadding"

    @Synchronized
    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec
                .Builder(
                    ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return gen.generateKey()
    }

    fun encrypt(plain: ByteArray): ByteArray {
        val c = Cipher.getInstance(TRANSFORM)
        c.init(Cipher.ENCRYPT_MODE, key())
        val iv = c.iv
        return byteArrayOf(iv.size.toByte()) + iv + c.doFinal(plain)
    }

    fun decrypt(blob: ByteArray): ByteArray {
        val n = blob[0].toInt()
        val c = Cipher.getInstance(TRANSFORM)
        c.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, blob, 1, n))
        return c.doFinal(blob, 1 + n, blob.size - 1 - n)
    }

    /** Atomic encrypted write: temp file + rename. */
    fun writeText(
        file: File,
        text: String,
    ) {
        file.parentFile?.mkdirs()
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeBytes(encrypt(text.toByteArray(Charsets.UTF_8)))
        if (!tmp.renameTo(file)) {
            file.delete()
            tmp.renameTo(file)
        }
    }

    /** Returns null if missing or undecryptable (e.g. key invalidated); caller treats as absent. */
    fun readText(file: File): String? {
        if (!file.exists()) return null
        return try {
            String(decrypt(file.readBytes()), Charsets.UTF_8)
        } catch (e: Exception) {
            null
        }
    }
}
