package com.audiplex.app.playback

import com.audiplex.app.data.api.TrackSchema
import com.audiplex.app.data.db.RecentlyPlayedTrackEntity

/**
 * Pure mapping between a live [MusicQueueItem] and a stored
 * [RecentlyPlayedTrackEntity], and back to the [TrackSchema] + album-lookup the
 * player needs to replay a remembered track (#3107/#993).
 *
 * Kept free of Android/Room types so the id<0 guard and the round-trip are
 * covered by a plain JVM unit test — the DAO's dedupe/trim SQL is exercised
 * on-device.
 */
object RecentlyPlayed {

    /**
     * A history row for a queue item at play-start, or null for non-catalog
     * items. Synthetic DJ voice-break clips carry a NEGATIVE track id and must
     * never enter play history — that is the only filter.
     */
    fun fromQueueItem(item: MusicQueueItem, playedAt: Long): RecentlyPlayedTrackEntity? {
        if (item.track.id < 0) return null
        return RecentlyPlayedTrackEntity(
            trackId = item.track.id,
            title = item.track.title,
            artistId = item.track.artistId,
            artistName = item.track.artistName,
            albumId = item.albumId,
            albumTitle = item.albumTitle,
            albumHasCover = item.albumHasCover,
            discNumber = item.track.discNumber,
            trackNumber = item.track.trackNumber,
            durationSeconds = item.track.durationSeconds,
            playedAt = playedAt
        )
    }

    fun toTrack(row: RecentlyPlayedTrackEntity): TrackSchema = TrackSchema(
        id = row.trackId,
        title = row.title,
        albumId = row.albumId,
        artistId = row.artistId,
        artistName = row.artistName,
        discNumber = row.discNumber,
        trackNumber = row.trackNumber,
        durationSeconds = row.durationSeconds
    )

    fun toTracks(rows: List<RecentlyPlayedTrackEntity>): List<TrackSchema> = rows.map { toTrack(it) }

    /**
     * album_id -> (album title, has cover) taken straight from the stored rows,
     * so playback needs no /api/music/albums round-trip to render artwork.
     */
    fun albumLookup(rows: List<RecentlyPlayedTrackEntity>): Map<Int, Pair<String, Boolean>> =
        rows.associate { it.albumId to (it.albumTitle to it.albumHasCover) }
}
