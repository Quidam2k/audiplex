package com.audiplex.app.data.db

import androidx.room.Dao
import androidx.room.Query
import androidx.room.Upsert
import kotlinx.coroutines.flow.Flow

@Dao
interface PlaybackPositionDao {

    @Query("SELECT * FROM playback_positions WHERE book_id = :bookId")
    suspend fun getById(bookId: Int): PlaybackPositionEntity?

    @Query("SELECT * FROM playback_positions WHERE book_id = :bookId")
    fun observeById(bookId: Int): Flow<PlaybackPositionEntity?>

    @Query("SELECT * FROM playback_positions WHERE synced_to_server = 0")
    suspend fun getUnsynced(): List<PlaybackPositionEntity>

    @Upsert
    suspend fun upsert(entity: PlaybackPositionEntity)

    @Query("UPDATE playback_positions SET synced_to_server = 1 WHERE book_id = :bookId AND updated_at = :updatedAt")
    suspend fun markSynced(bookId: Int, updatedAt: Long)
}
