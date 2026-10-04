package com.familypulse.familypulse.collector

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import java.util.concurrent.TimeUnit
import kotlin.random.Random

/**
 * Background collection, independent of any Flutter view or engine.
 *
 * Periodic work is requested every 15 minutes (the WorkManager minimum) with a network
 * constraint. Execution is INEXACT: Doze, App Standby buckets, battery saver and OEM
 * restrictions can delay runs by hours. Failed uploads schedule a one-off retry with
 * exponential backoff plus random initial jitter.
 */
class SyncWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val trigger = inputData.getString("trigger") ?: "background"
        val summary = SyncEngine(applicationContext).run(trigger)
        return when (summary.outcome) {
            SyncEngine.Outcome.RETRY -> {
                if (runAttemptCount < 6) Result.retry() else Result.success()
            }

            else -> {
                Result.success()
            }
        }
    }

    companion object {
        const val PERIODIC = "familypulse-periodic-sync"
        const val RETRY = "familypulse-retry-sync"

        private val network = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

        fun schedulePeriodic(context: Context) {
            val req =
                PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
                    .setConstraints(network)
                    .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 60, TimeUnit.SECONDS)
                    .setInitialDelay(Random.nextLong(0, 60), TimeUnit.SECONDS)
                    .setInputData(workDataOf("trigger" to "background"))
                    .build()
            WorkManager
                .getInstance(context)
                .enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.UPDATE, req)
        }

        fun scheduleRetry(context: Context) {
            val req =
                OneTimeWorkRequestBuilder<SyncWorker>()
                    .setConstraints(network)
                    .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                    .setInitialDelay(30 + Random.nextLong(0, 30), TimeUnit.SECONDS)
                    .setInputData(workDataOf("trigger" to "background"))
                    .build()
            WorkManager.getInstance(context).enqueueUniqueWork(RETRY, ExistingWorkPolicy.KEEP, req)
        }

        fun cancelAll(context: Context) {
            val wm = WorkManager.getInstance(context)
            wm.cancelUniqueWork(PERIODIC)
            wm.cancelUniqueWork(RETRY)
        }
    }
}
