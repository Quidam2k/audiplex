package com.audiplex.app.data.db

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * Local-first playback position for a book. Stored for EVERY book (not just
 * downloads) so resume works instantly and offline. Reconciled with the
 * server's PlaybackPosition table using latest-wins on [updatedAt].
 */
@Entity(tableName = "playback_positions")
data class PlaybackPositionEntity(
    @PrimaryKey
    @ColumnInfo(name = "book_id") val bookId: Int,
    @ColumnInfo(name = "position_seconds") val positionSeconds: Double,
    @ColumnInfo(name = "chapter_index") val chapterIndex: Int,
    @ColumnInfo(name = "is_finished") val isFinished: Boolean = false,
    // Epoch millis (wall-clock) — drives latest-wins reconciliation with the server.
    @ColumnInfo(name = "updated_at") val updatedAt: Long = System.currentTimeMillis(),
    // false until a PUT to the server succeeds — drives the offline→online flush.
    @ColumnInfo(name = "synced_to_server") val syncedToServer: Boolean = false
)
