package com.audiplex.app.playback

import com.audiplex.app.data.api.TrackSchema
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The pure history mapping (#3107/#993): queue item -> stored row -> replayable
 * track + album lookup, and the guard that keeps synthetic DJ voice-break clips
 * (negative track ids) out of play history. The DAO's dedupe/trim SQL is
 * exercised on-device.
 */
class RecentlyPlayedMappingTest {

    private fun queueItem(
        id: Int,
        title: String = "Song",
        albumId: Int = 7,
        albumTitle: String = "The Album",
        albumHasCover: Boolean = true,
        artistName: String? = "The Artist"
    ) = MusicQueueItem(
        track = TrackSchema(
            id = id,
            title = title,
            albumId = albumId,
            artistId = 3,
            artistName = artistName,
            discNumber = 1,
            trackNumber = 4,
            durationSeconds = 210.0
        ),
        albumId = albumId,
        albumTitle = albumTitle,
        albumHasCover = albumHasCover
    )

    @Test
    fun `catalog track maps to a full history row`() {
        val row = RecentlyPlayed.fromQueueItem(queueItem(id = 42), playedAt = 1000L)!!
        assertEquals(42, row.trackId)
        assertEquals("Song", row.title)
        assertEquals(3, row.artistId)
        assertEquals("The Artist", row.artistName)
        assertEquals(7, row.albumId)
        assertEquals("The Album", row.albumTitle)
        assertTrue(row.albumHasCover)
        assertEquals(1, row.discNumber)
        assertEquals(4, row.trackNumber)
        assertEquals(210.0, row.durationSeconds, 0.0)
        assertEquals(1000L, row.playedAt)
    }

    @Test
    fun `synthetic DJ clip is never recorded`() {
        // Voice-break clips carry a session-unique negative id.
        assertNull(RecentlyPlayed.fromQueueItem(queueItem(id = -1), playedAt = 1000L))
        assertNull(RecentlyPlayed.fromQueueItem(queueItem(id = -9999), playedAt = 1000L))
    }

    @Test
    fun `id zero is a real track and is recorded`() {
        // The guard is strictly negative — 0 is a valid catalog id, not synthetic.
        assertEquals(0, RecentlyPlayed.fromQueueItem(queueItem(id = 0), playedAt = 1L)!!.trackId)
    }

    @Test
    fun `row round-trips back to a replayable track`() {
        val row = RecentlyPlayed.fromQueueItem(queueItem(id = 42), playedAt = 1000L)!!
        val track = RecentlyPlayed.toTrack(row)
        assertEquals(42, track.id)
        assertEquals("Song", track.title)
        assertEquals(7, track.albumId)
        assertEquals(3, track.artistId)
        assertEquals("The Artist", track.artistName)
        assertEquals(1, track.discNumber)
        assertEquals(4, track.trackNumber)
        assertEquals(210.0, track.durationSeconds, 0.0)
    }

    @Test
    fun `album lookup is built from the rows without an api call`() {
        val rows = listOf(
            RecentlyPlayed.fromQueueItem(queueItem(id = 1, albumId = 7, albumTitle = "A", albumHasCover = true), 1L)!!,
            RecentlyPlayed.fromQueueItem(queueItem(id = 2, albumId = 9, albumTitle = "B", albumHasCover = false), 2L)!!
        )
        val lookup = RecentlyPlayed.albumLookup(rows)
        assertEquals("A" to true, lookup[7])
        assertEquals("B" to false, lookup[9])
        assertEquals(2, lookup.size)
    }

    @Test
    fun `toTracks preserves order`() {
        val rows = listOf(
            RecentlyPlayed.fromQueueItem(queueItem(id = 5), 3L)!!,
            RecentlyPlayed.fromQueueItem(queueItem(id = 6), 2L)!!,
            RecentlyPlayed.fromQueueItem(queueItem(id = 7), 1L)!!
        )
        assertEquals(listOf(5, 6, 7), RecentlyPlayed.toTracks(rows).map { it.id })
    }
}
