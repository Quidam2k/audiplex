package com.audiplex.app.data.db

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "downloads")
data class DownloadEntity(
    @PrimaryKey
    @ColumnInfo(name = "book_id") val bookId: Int,
    val title: String,
    val author: String?,
    @ColumnInfo(name = "file_size") val fileSize: Long,
    @ColumnInfo(name = "downloaded_bytes") val downloadedBytes: Long = 0,
    val status: String = Status.PENDING,
    @ColumnInfo(name = "local_file_path") val localFilePath: String,
    @ColumnInfo(name = "book_metadata_json") val bookMetadataJson: String,
    @ColumnInfo(name = "error_message") val errorMessage: String? = null,
    @ColumnInfo(name = "created_at") val createdAt: Long = System.currentTimeMillis(),
    @ColumnInfo(name = "updated_at") val updatedAt: Long = System.currentTimeMillis()
) {
    object Status {
        const val PENDING = "PENDING"
        const val DOWNLOADING = "DOWNLOADING"
        const val PAUSED = "PAUSED"
        const val COMPLETED = "COMPLETED"
        const val FAILED = "FAILED"
    }
}
