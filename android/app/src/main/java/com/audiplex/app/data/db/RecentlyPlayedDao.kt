package com.audiplex.app.data.db

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.Query
import androidx.room.Transaction
import kotlinx.coroutines.flow.Flow

@Dao
interface RecentlyPlayedDao {

    @Query("SELECT * FROM recently_played_tracks ORDER BY played_at DESC, id DESC LIMIT :limit")
    fun observeRecent(limit: Int): Flow<List<RecentlyPlayedTrackEntity>>

    @Insert
    suspend fun insert(entity: RecentlyPlayedTrackEntity)

    @Query("DELETE FROM recently_played_tracks WHERE track_id = :trackId")
    suspend fun deleteByTrackId(trackId: Int)

    @Query(
        "DELETE FROM recently_played_tracks WHERE id NOT IN " +
            "(SELECT id FROM recently_played_tracks ORDER BY played_at DESC, id DESC LIMIT :keep)"
    )
    suspend fun trimTo(keep: Int)

    @Query("DELETE FROM recently_played_tracks")
    suspend fun clear()

    /**
     * Record a track "start" as one row per track, newest wins. Deleting any
     * prior row for the same trackId before inserting keeps the list from
     * filling with the same track on repeat / DJ loops; trimTo bounds the table
     * so history never grows without limit. Both steps run in one transaction
     * so a concurrent read never sees the between-state.
     */
    @Transaction
    suspend fun record(entity: RecentlyPlayedTrackEntity, keep: Int = 100) {
        deleteByTrackId(entity.trackId)
        insert(entity)
        trimTo(keep)
    }
}
