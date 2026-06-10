package com.audiplex.app.data.download

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.hilt.work.HiltWorker
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import com.audiplex.app.R
import com.audiplex.app.data.db.DownloadDao
import com.audiplex.app.data.db.DownloadEntity
import dagger.assisted.Assisted
import dagger.assisted.AssistedInject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.TimeUnit

@HiltWorker
class DownloadWorker @AssistedInject constructor(
    @Assisted context: Context,
    @Assisted params: WorkerParameters,
    private val downloadDao: DownloadDao,
    apiOkHttpClient: OkHttpClient
) : CoroutineWorker(context, params) {

    companion object {
        const val KEY_BOOK_ID = "book_id"
        const val KEY_BASE_URL = "base_url"
        private const val CHANNEL_ID = "downloads"
        private const val NOTIFICATION_ID_BASE = 20000
        private const val CHUNK_SIZE = 64 * 1024
        private const val PROGRESS_UPDATE_THRESHOLD = 1024 * 1024L
    }

    // Derived from the shared API client so the auth interceptor attaches the
    // Bearer token (/api/stream requires it), with timeouts sized for
    // multi-hundred-MB audiobook files.
    private val httpClient = apiOkHttpClient.newBuilder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.MINUTES)
        .build()

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val bookId = inputData.getInt(KEY_BOOK_ID, -1)
        val baseUrl = inputData.getString(KEY_BASE_URL) ?: return@withContext Result.failure()
        if (bookId < 0) return@withContext Result.failure()

        val entity = downloadDao.getById(bookId) ?: return@withContext Result.failure()
        val file = File(entity.localFilePath)
        file.parentFile?.mkdirs()

        downloadDao.updateStatus(bookId, DownloadEntity.Status.DOWNLOADING)

        try {
            setForeground(createForegroundInfo(entity.title, 0, entity.fileSize))

            var existingBytes = if (file.exists()) file.length() else 0L
            if (entity.fileSize > 0 && existingBytes >= entity.fileSize) {
                downloadDao.updateProgress(bookId, existingBytes)
                downloadDao.updateStatus(bookId, DownloadEntity.Status.COMPLETED)
                return@withContext Result.success()
            }

            val requestBuilder = Request.Builder()
                .url("${baseUrl.trimEnd('/')}/api/stream/$bookId")

            if (existingBytes > 0) {
                requestBuilder.addHeader("Range", "bytes=$existingBytes-")
            }

            httpClient.newCall(requestBuilder.build()).execute().use { response ->
                if (!response.isSuccessful) {
                    downloadDao.updateStatus(bookId, DownloadEntity.Status.FAILED, "HTTP ${response.code}")
                    return@withContext Result.retry()
                }

                // We asked for a range but got the whole file — start over from 0
                // instead of appending a full copy after the partial bytes.
                if (existingBytes > 0 && response.code != 206) {
                    existingBytes = 0L
                }

                val body = response.body ?: run {
                    downloadDao.updateStatus(bookId, DownloadEntity.Status.FAILED, "Empty response")
                    return@withContext Result.retry()
                }

                var totalWritten = existingBytes
                var lastProgressUpdate = totalWritten

                body.byteStream().use { input ->
                    // Append mode when resuming — FileOutputStream(file) would
                    // truncate and leave a zero-filled hole before the new bytes.
                    FileOutputStream(file, existingBytes > 0).use { output ->
                        val buffer = ByteArray(CHUNK_SIZE)
                        while (true) {
                            if (isStopped) {
                                downloadDao.updateProgress(bookId, totalWritten)
                                downloadDao.updateStatus(bookId, DownloadEntity.Status.PAUSED)
                                return@withContext Result.success()
                            }
                            val bytesRead = input.read(buffer)
                            if (bytesRead == -1) break
                            output.write(buffer, 0, bytesRead)
                            totalWritten += bytesRead

                            if (totalWritten - lastProgressUpdate >= PROGRESS_UPDATE_THRESHOLD) {
                                downloadDao.updateProgress(bookId, totalWritten)
                                lastProgressUpdate = totalWritten
                                setForeground(createForegroundInfo(entity.title, totalWritten, entity.fileSize))
                            }
                        }
                    }
                }

                downloadDao.updateProgress(bookId, totalWritten)
                downloadDao.updateStatus(bookId, DownloadEntity.Status.COMPLETED)
                Result.success()
            }
        } catch (e: Exception) {
            val currentBytes = if (file.exists()) file.length() else 0L
            downloadDao.updateProgress(bookId, currentBytes)
            downloadDao.updateStatus(bookId, DownloadEntity.Status.FAILED, e.message)
            Result.retry()
        }
    }

    private fun createForegroundInfo(title: String, downloaded: Long, total: Long): ForegroundInfo {
        val nm = applicationContext.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (nm.getNotificationChannel(CHANNEL_ID) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "Downloads", NotificationManager.IMPORTANCE_LOW)
            )
        }

        val progress = if (total > 0) ((downloaded * 100) / total).toInt() else 0
        val notification = NotificationCompat.Builder(applicationContext, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle("Downloading $title")
            .setProgress(100, progress, downloaded == 0L)
            .setOngoing(true)
            .setSilent(true)
            .build()

        val notificationId = NOTIFICATION_ID_BASE + inputData.getInt(KEY_BOOK_ID, 0)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            ForegroundInfo(notificationId, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            ForegroundInfo(notificationId, notification)
        }
    }
}
