package com.audiplex.app.data.download

import android.content.Context
import android.util.Log
import androidx.hilt.work.HiltWorker
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.data.api.BookSummary
import com.audiplex.app.data.db.DownloadEntity
import com.audiplex.app.ui.meditations.MeditationLists
import dagger.assisted.Assisted
import dagger.assisted.AssistedInject
import kotlinx.coroutines.flow.first
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

/**
 * #5462: gets every meditation onto the phone. The Download all button and the background
 * auto-download both go through queueMissing, so they skip and retry the same way (#845).
 */
@Singleton
class MeditationSync @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val downloadRepository: DownloadRepository,
    private val settingsStore: SettingsStore
) {
    data class Outcome(val queued: Int, val failed: Int)

    /** Queue each of [books] that isn't already on the phone or downloading. */
    suspend fun queueMissing(books: List<BookSummary>, unmeteredOnly: Boolean): Outcome {
        val api = apiHolder.api ?: return Outcome(0, books.size)
        val baseUrl = apiHolder.baseUrl
        val states = downloadRepository.observeAllDownloads().first().associate { it.bookId to it.status }
        var queued = 0
        var failed = 0
        for (book in books) {
            try {
                // #845: never re-fetch a copy that is already here; that delete-and-refetch was the flicker.
                val status = states[book.id]
                val filePresent = status == DownloadEntity.Status.COMPLETED &&
                    downloadRepository.getLocalPath(book.id) != null
                val workActive = (status == DownloadEntity.Status.PENDING ||
                    status == DownloadEntity.Status.DOWNLOADING) &&
                    downloadRepository.isWorkActive(book.id)
                if (!MeditationLists.shouldQueue(status, filePresent, workActive)) continue
                // startDownload needs the full detail (file size + metadata JSON for offline display).
                downloadRepository.startDownload(api.getBook(book.id), baseUrl, unmeteredOnly)
                queued++
            } catch (_: Exception) {
                failed++
            }
        }
        return Outcome(queued, failed)
    }

    /**
     * #5462: the background pass. Does nothing when the setting is off, there's no server or
     * login, or the sleep-bed list can't be fetched: unlike the screen, which then shows every
     * row as a meditation, this must never download a sleep bed.
     */
    suspend fun autoSync(): Outcome? {
        if (!settingsStore.autoDownloadMeditations.first()) return null
        settingsStore.serverUrl.first().takeIf { it.isNotBlank() }?.let { apiHolder.setBaseUrlIfUnset(it) }
        if (settingsStore.authToken.first().isBlank()) return null
        val api = apiHolder.api ?: return null
        val books = api.getBooks(category = CATEGORY_MEDITATION)
        val bedIds = try { api.getSleepBeds().map { it.id }.toSet() } catch (_: Exception) { return null }
        return queueMissing(MeditationLists.autoCandidates(books, bedIds), unmeteredOnly = true)
    }

    companion object {
        const val TAG = "MeditationSync"
        const val PERIODIC_NAME = "meditation_sync"
        const val NOW_NAME = "meditation_sync_now"

        /** #5462: every 15 min plus once now, Wi-Fi (unmetered) only. Safe to call on every start. */
        fun schedule(context: Context) {
            try {
                val constraints = Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.UNMETERED)
                    .build()
                val wm = WorkManager.getInstance(context)
                wm.enqueueUniquePeriodicWork(
                    PERIODIC_NAME, ExistingPeriodicWorkPolicy.KEEP,
                    PeriodicWorkRequestBuilder<MeditationSyncWorker>(15, TimeUnit.MINUTES)
                        .setConstraints(constraints).build()
                )
                wm.enqueueUniqueWork(
                    NOW_NAME, ExistingWorkPolicy.KEEP,
                    OneTimeWorkRequestBuilder<MeditationSyncWorker>().setConstraints(constraints).build()
                )
            } catch (e: Exception) {
                Log.w(TAG, "Could not schedule meditation sync", e)
            }
        }
    }
}

/** #5462: runs MeditationSync.autoSync. Always succeeds, so a bad pass never sets off backoff. */
@HiltWorker
class MeditationSyncWorker @AssistedInject constructor(
    @Assisted context: Context,
    @Assisted params: WorkerParameters,
    private val sync: MeditationSync
) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        try {
            val outcome = sync.autoSync()
            if (outcome != null && (outcome.queued > 0 || outcome.failed > 0)) {
                Log.i(MeditationSync.TAG, "auto-download queued=${outcome.queued} failed=${outcome.failed}")
            }
        } catch (e: Exception) {
            Log.w(MeditationSync.TAG, "auto-download pass failed", e)
        }
        return Result.success()
    }
}
