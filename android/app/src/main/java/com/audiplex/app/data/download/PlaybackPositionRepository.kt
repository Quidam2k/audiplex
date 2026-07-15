package com.audiplex.app.data.download

import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.api.ProgressSchema
import com.audiplex.app.data.api.ProgressUpdate
import com.audiplex.app.data.db.PlaybackPositionDao
import com.audiplex.app.data.db.PlaybackPositionEntity
import java.time.Instant
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneOffset
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Resolved resume target for a book, after reconciling the local Room copy
 * against the server. [hasProgress] is false when neither side has anything
 * (fresh book → start at 0:00).
 */
data class ResumeInfo(
    val positionSeconds: Double,
    val chapterIndex: Int,
    val isFinished: Boolean,
    val hasProgress: Boolean
) {
    companion object {
        val NONE = ResumeInfo(0.0, 0, isFinished = false, hasProgress = false)
    }
}

/**
 * Local-first playback position store. Room is the source of truth for resume
 * (instant, offline-proof); the server is reconciled with latest-wins on
 * wall-clock `updated_at`. Single-user, so clock skew between phone and server
 * is an accepted trade-off (no vector clocks).
 */
@Singleton
class PlaybackPositionRepository @Inject constructor(
    private val positionDao: PlaybackPositionDao,
    private val apiHolder: ApiServiceHolder
) {

    /** Local save used by PlaybackManager. Marks the row unsynced by default. */
    suspend fun saveLocal(
        bookId: Int,
        positionSeconds: Double,
        chapterIndex: Int,
        isFinished: Boolean,
        updatedAt: Long = System.currentTimeMillis(),
        syncedToServer: Boolean = false
    ) {
        positionDao.upsert(
            PlaybackPositionEntity(
                bookId = bookId,
                positionSeconds = positionSeconds,
                chapterIndex = chapterIndex,
                isFinished = isFinished,
                updatedAt = updatedAt,
                syncedToServer = syncedToServer
            )
        )
    }

    suspend fun markSynced(bookId: Int, updatedAt: Long) = positionDao.markSynced(bookId, updatedAt)

    /**
     * Read local + attempt server, pick the newer by `updated_at`, propagate the
     * winner to the loser, and return the resume target. Offline / server 404 →
     * use whatever local has. Neither → [ResumeInfo.NONE] (0:00).
     */
    suspend fun resolveStartPosition(bookId: Int): ResumeInfo {
        val local = positionDao.getById(bookId)
        val server = try { apiHolder.api?.getProgress(bookId) } catch (_: Exception) { null }
        val serverUpdatedAt = server?.let { parseIso(it.updatedAt) }

        return when {
            local == null && server == null -> ResumeInfo.NONE

            // Only local (offline, or server has nothing yet). Push it up if we can.
            server == null -> {
                if (!local!!.syncedToServer) pushToServer(local)
                local.toResumeInfo()
            }

            // Only server. Cache it locally as already-synced.
            local == null -> {
                positionDao.upsert(server.toEntity(serverUpdatedAt ?: System.currentTimeMillis(), synced = true))
                server.toResumeInfo()
            }

            // Both present — latest-wins.
            serverUpdatedAt != null && serverUpdatedAt > local.updatedAt -> {
                positionDao.upsert(server.toEntity(serverUpdatedAt, synced = true))
                server.toResumeInfo()
            }

            else -> {
                if (!local.syncedToServer) pushToServer(local)
                local.toResumeInfo()
            }
        }
    }

    /** PUT every locally-unsynced row to the server. Called opportunistically when online. */
    suspend fun flushUnsynced() {
        val api = apiHolder.api ?: return
        for (row in positionDao.getUnsynced()) {
            try {
                api.updateProgress(
                    row.bookId,
                    ProgressUpdate(
                        positionSeconds = row.positionSeconds,
                        chapterIndex = row.chapterIndex,
                        isFinished = row.isFinished
                    )
                )
                positionDao.markSynced(row.bookId, row.updatedAt)
            } catch (_: Exception) {
                // Still offline for this row — leave it unsynced, retry next flush.
            }
        }
    }

    private suspend fun pushToServer(local: PlaybackPositionEntity) {
        val api = apiHolder.api ?: return
        try {
            api.updateProgress(
                local.bookId,
                ProgressUpdate(
                    positionSeconds = local.positionSeconds,
                    chapterIndex = local.chapterIndex,
                    isFinished = local.isFinished
                )
            )
            positionDao.markSynced(local.bookId, local.updatedAt)
        } catch (_: Exception) { }
    }

    private fun PlaybackPositionEntity.toResumeInfo() =
        ResumeInfo(positionSeconds, chapterIndex, isFinished, hasProgress = true)

    private fun ProgressSchema.toResumeInfo() =
        ResumeInfo(positionSeconds, chapterIndex, isFinished, hasProgress = true)

    private fun ProgressSchema.toEntity(updatedAt: Long, synced: Boolean) =
        PlaybackPositionEntity(
            bookId = bookId,
            positionSeconds = positionSeconds,
            chapterIndex = chapterIndex,
            isFinished = isFinished,
            updatedAt = updatedAt,
            syncedToServer = synced
        )

    /**
     * Parse the server's ISO-8601 `updated_at` to epoch millis. Handles both an
     * explicit offset (`...+00:00` / `...Z`) and a naive timestamp (SQLite can
     * drop the tz on round-trip) — naive is treated as UTC. Returns null if
     * unparseable, so reconciliation falls back to local.
     */
    private fun parseIso(value: String): Long? = try {
        OffsetDateTime.parse(value).toInstant().toEpochMilli()
    } catch (_: Exception) {
        try {
            LocalDateTime.parse(value).toInstant(ZoneOffset.UTC).toEpochMilli()
        } catch (_: Exception) {
            try { Instant.parse(value).toEpochMilli() } catch (_: Exception) { null }
        }
    }
}
