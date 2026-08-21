package com.audiplex.app.data.db

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * One "recently played" history row (#3107/#993). Written when a music track
 * STARTS (see PlaybackManager). The full track identity is stored — not just an
 * id — so the Recently-played list renders and REPLAYS with no extra API call:
 * album title + cover flag come straight off the row.
 *
 * Only catalog tracks land here; synthetic DJ voice-break clips (negative track
 * ids) are filtered out at the call site (RecentlyPlayed.fromQueueItem).
 */
@Entity(tableName = "recently_played_tracks")
data class RecentlyPlayedTrackEntity(
    @PrimaryKey(autoGenerate = true)
    @ColumnInfo(name = "id") val id: Long = 0,
    @ColumnInfo(name = "track_id") val trackId: Int,
    @ColumnInfo(name = "title") val title: String,
    @ColumnInfo(name = "artist_id") val artistId: Int,
    @ColumnInfo(name = "artist_name") val artistName: String?,
    @ColumnInfo(name = "album_id") val albumId: Int,
    @ColumnInfo(name = "album_title") val albumTitle: String,
    @ColumnInfo(name = "album_has_cover") val albumHasCover: Boolean,
    @ColumnInfo(name = "disc_number") val discNumber: Int,
    @ColumnInfo(name = "track_number") val trackNumber: Int,
    @ColumnInfo(name = "duration_seconds") val durationSeconds: Double,
    // Epoch millis (wall-clock) the track started — drives the newest-first sort.
    @ColumnInfo(name = "played_at") val playedAt: Long
)
