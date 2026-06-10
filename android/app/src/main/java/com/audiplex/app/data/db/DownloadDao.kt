package com.audiplex.app.data.db

import androidx.room.Dao
import androidx.room.Query
import androidx.room.Upsert
import kotlinx.coroutines.flow.Flow

@Dao
interface DownloadDao {

    @Query("SELECT * FROM downloads WHERE book_id = :bookId")
    suspend fun getById(bookId: Int): DownloadEntity?

    @Query("SELECT * FROM downloads ORDER BY created_at DESC")
    suspend fun getAll(): List<DownloadEntity>

    @Query("SELECT * FROM downloads WHERE book_id = :bookId")
    fun observeById(bookId: Int): Flow<DownloadEntity?>

    @Query("SELECT * FROM downloads ORDER BY created_at DESC")
    fun observeAll(): Flow<List<DownloadEntity>>

    @Upsert
    suspend fun upsert(entity: DownloadEntity)

    @Query("UPDATE downloads SET downloaded_bytes = :bytes, updated_at = :updatedAt WHERE book_id = :bookId")
    suspend fun updateProgress(bookId: Int, bytes: Long, updatedAt: Long = System.currentTimeMillis())

    @Query("UPDATE downloads SET status = :status, error_message = :errorMessage, updated_at = :updatedAt WHERE book_id = :bookId")
    suspend fun updateStatus(bookId: Int, status: String, errorMessage: String? = null, updatedAt: Long = System.currentTimeMillis())

    @Query("DELETE FROM downloads WHERE book_id = :bookId")
    suspend fun delete(bookId: Int)

    @Query("SELECT COALESCE(SUM(file_size), 0) FROM downloads WHERE status = 'COMPLETED'")
    fun observeTotalStorageUsed(): Flow<Long>
}
