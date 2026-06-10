package com.audiplex.app.data.download

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.workDataOf
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.data.api.BookDetail
import com.audiplex.app.data.db.DownloadDao
import com.audiplex.app.data.db.DownloadEntity
import com.squareup.moshi.Moshi
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import java.io.File
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class DownloadRepository @Inject constructor(
    @ApplicationContext private val context: Context,
    private val downloadDao: DownloadDao,
    private val settingsStore: SettingsStore,
    private val moshi: Moshi
) {
    private val downloadsDir = context.getExternalFilesDir("downloads")

    suspend fun startDownload(book: BookDetail, baseUrl: String) {
        val filePath = File(downloadsDir, "${book.id}.m4b").absolutePath
        val metadataJson = moshi.adapter(BookDetail::class.java).toJson(book)
        val entity = DownloadEntity(
            bookId = book.id,
            title = book.title,
            author = book.author,
            fileSize = book.fileSize,
            localFilePath = filePath,
            bookMetadataJson = metadataJson
        )
        downloadDao.upsert(entity)
        enqueueWork(book.id, baseUrl)
    }

    suspend fun pauseDownload(bookId: Int) {
        WorkManager.getInstance(context).cancelUniqueWork(workName(bookId))
        downloadDao.updateStatus(bookId, DownloadEntity.Status.PAUSED)
    }

    suspend fun resumeDownload(bookId: Int, baseUrl: String) {
        downloadDao.updateStatus(bookId, DownloadEntity.Status.PENDING)
        enqueueWork(bookId, baseUrl)
    }

    suspend fun deleteDownload(bookId: Int) {
        WorkManager.getInstance(context).cancelUniqueWork(workName(bookId))
        val entity = downloadDao.getById(bookId)
        if (entity != null) {
            File(entity.localFilePath).delete()
        }
        downloadDao.delete(bookId)
    }

    suspend fun deleteAll() {
        val all = downloadDao.getAll()
        for (entity in all) {
            WorkManager.getInstance(context).cancelUniqueWork(workName(entity.bookId))
            File(entity.localFilePath).delete()
        }
        for (entity in all) {
            downloadDao.delete(entity.bookId)
        }
    }

    fun observeDownload(bookId: Int): Flow<DownloadEntity?> = downloadDao.observeById(bookId)

    fun observeAllDownloads(): Flow<List<DownloadEntity>> = downloadDao.observeAll()

    suspend fun getLocalPath(bookId: Int): String? {
        val entity = downloadDao.getById(bookId) ?: return null
        if (entity.status != DownloadEntity.Status.COMPLETED) return null
        val file = File(entity.localFilePath)
        if (!file.exists()) return null
        return entity.localFilePath
    }

    fun getBookDetailFromJson(json: String): BookDetail? =
        try { moshi.adapter(BookDetail::class.java).fromJson(json) } catch (_: Exception) { null }

    fun totalStorageUsed(): Flow<Long> = downloadDao.observeTotalStorageUsed()

    private suspend fun enqueueWork(bookId: Int, baseUrl: String) {
        val cellular = settingsStore.downloadOnCellular.first()
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(if (cellular) NetworkType.CONNECTED else NetworkType.UNMETERED)
            .build()

        val request = OneTimeWorkRequestBuilder<DownloadWorker>()
            .setInputData(workDataOf(
                DownloadWorker.KEY_BOOK_ID to bookId,
                DownloadWorker.KEY_BASE_URL to baseUrl
            ))
            .setConstraints(constraints)
            .build()

        WorkManager.getInstance(context)
            .enqueueUniqueWork(workName(bookId), ExistingWorkPolicy.REPLACE, request)
    }

    private fun workName(bookId: Int) = "download_$bookId"
}
