package com.familypulse.familypulse

import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import androidx.activity.result.ActivityResultLauncher
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import com.familypulse.familypulse.collector.CollectorConfig
import com.familypulse.familypulse.collector.HealthConnectReader
import com.familypulse.familypulse.collector.SessionStore
import com.familypulse.familypulse.collector.SyncEngine
import com.familypulse.familypulse.collector.SyncWorker
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.IOException

/**
 * FlutterFragmentActivity (a ComponentActivity) so the Health Connect permission contract
 * can be registered. The MethodChannel exposes setup, "Sync now" and diagnostics; background
 * collection itself runs in [SyncWorker] and never needs this activity or a Flutter engine.
 */
class MainActivity : FlutterFragmentActivity() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var pendingPermissionResult: MethodChannel.Result? = null

    private val permissionLauncher: ActivityResultLauncher<Set<String>> =
        registerForActivityResult(PermissionController.createRequestPermissionResultContract()) { granted ->
            pendingPermissionResult?.success(granted.toList())
            pendingPermissionResult = null
        }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler { call, result -> handle(call, result) }
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun handle(
        call: MethodCall,
        result: MethodChannel.Result,
    ) {
        val ctx = applicationContext
        val config = CollectorConfig(ctx)
        val engine = SyncEngine(ctx)
        val reader = HealthConnectReader(ctx)
        when (call.method) {
            "launchAction" -> {
                result.success(intent?.action)
            }

            "installationId" -> {
                result.success(config.installationId)
            }

            "healthConnectStatus" -> {
                result.success(reader.sdkStatus())
            }

            "status" -> {
                scope.launch {
                    val s = engine.status()
                    try {
                        s.put("capabilities", reader.capabilities(appVersion()))
                    } catch (e: Exception) {
                        s.put("capabilities_error", e.javaClass.simpleName)
                    }
                    result.success(s.toString())
                }
            }

            "requestPermissions" -> {
                if (reader.sdkStatus() != "available") {
                    result.error("unavailable", "Health Connect not available", null)
                    return
                }
                if (pendingPermissionResult != null) {
                    result.error("busy", "permission request in progress", null)
                    return
                }
                val perms = HealthConnectReader.READ_PERMISSIONS.values.toMutableSet()
                if (call.argument<Boolean>("includeBackground") == true && reader.backgroundFeatureAvailable()) {
                    perms += HealthConnectReader.BACKGROUND_PERMISSION
                }
                pendingPermissionResult = result
                permissionLauncher.launch(perms)
            }

            "openHealthConnect" -> {
                val i =
                    if (reader.sdkStatus() == "available") {
                        HealthConnectClient.getHealthConnectManageDataIntent(ctx)
                    } else {
                        Intent(Intent.ACTION_VIEW).apply {
                            setPackage("com.android.vending")
                            data =
                                Uri.parse(
                                    "market://details?id=${HealthConnectReader.PROVIDER_PACKAGE}&url=healthconnect%3A%2F%2Fonboarding",
                                )
                            putExtra("overlay", true)
                            putExtra("callerId", packageName)
                        }
                    }
                try {
                    startActivity(i)
                    result.success(true)
                } catch (e: ActivityNotFoundException) {
                    result.success(false)
                }
            }

            "configure" -> {
                config.apiBaseUrl = call.argument<String>("apiBaseUrl")
                config.profileId = call.argument<String>("profileId")
                config.deviceId = call.argument<String>("deviceId")
                result.success(true)
            }

            "setSession" -> {
                val json = call.argument<String>("session")
                SessionStore(ctx).save(json?.let { SessionStore.Session.fromJson(JSONObject(it)) })
                result.success(true)
            }

            "accessToken" -> {
                scope.launch {
                    try {
                        result.success(SessionStore(ctx).accessToken())
                    } catch (e: SessionStore.AuthRequired) {
                        result.error("auth_required", "sign in again", null)
                    } catch (e: IOException) {
                        result.error("network", e.message, null)
                    }
                }
            }

            "enable" -> {
                config.enabled = true
                SyncWorker.schedulePeriodic(ctx)
                result.success(true)
            }

            "disable" -> {
                engine.disableAndClear(clearSession = false)
                result.success(true)
            }

            "signOut" -> {
                engine.disableAndClear(clearSession = true)
                result.success(true)
            }

            "syncNow" -> {
                scope.launch {
                    val summary = engine.run("foreground")
                    if (summary.outcome == SyncEngine.Outcome.RETRY) SyncWorker.scheduleRetry(ctx)
                    result.success(summary.json.put("outcome", summary.outcome.name).toString())
                }
            }

            "diagnose" -> {
                scope.launch {
                    try {
                        result.success(engine.diagnose((call.argument<Int>("hours") ?: 24).toLong()).toString())
                    } catch (e: Exception) {
                        result.error("diagnose_failed", e.javaClass.simpleName, null)
                    }
                }
            }

            else -> {
                result.notImplemented()
            }
        }
    }

    private fun appVersion(): String =
        try {
            packageManager.getPackageInfo(packageName, 0).versionName ?: "?"
        } catch (e: Exception) {
            "?"
        }

    companion object {
        const val CHANNEL = "familypulse/collector"
    }
}
