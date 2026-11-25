package com.audiplex.app.playback

import com.audiplex.app.data.api.TrackSchema
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertSame
import org.junit.Test

/** #6999: Recently played swaps in the server's current size + trim, so its cache key is never stale. */
class RecentlyPlayedFreshTest {

    private fun t(id: Int, size: Long? = null, trim: Double? = null) =
        TrackSchema(id, "T$id", 1, 1, "A", 1, id, 100.0, null, size, trim)

    @Test
    fun freshWins() {
        val stored = listOf(t(8), t(3))
        val fresh = listOf(t(3, 500L, null), t(8, 2731107L, 144.0))
        val merged = RecentlyPlayed.withFresh(stored, fresh)
        assertEquals(listOf(8, 3), merged.map { it.id })
        assertEquals(2731107L, merged[0].fileSize)
        assertEquals(144.0, merged[0].endTrimSeconds)
    }

    @Test
    fun missingIdKeepsStoredRow() {
        val stored = listOf(t(1), t(2), t(3))
        val fresh = listOf(t(2, 10L, 1.0))
        val merged = RecentlyPlayed.withFresh(stored, fresh)
        assertEquals(listOf(1, 2, 3), merged.map { it.id })
        assertSame(stored[0], merged[0])
        assertSame(stored[2], merged[2])
        assertEquals(10L, merged[1].fileSize)
    }

    @Test
    fun emptyFreshLeavesStoredUnchanged() {
        val stored = listOf(t(1), t(2, 5L, 2.0), t(3))
        assertEquals(stored, RecentlyPlayed.withFresh(stored, emptyList()))
    }

    @Test
    fun cacheKeyChangesAfterMergeAndRecut() {
        val u = "http://h/api/music/stream/track/8"
        val stored = t(8)
        val storedKey = StreamCache.musicCacheKey(u, stored.fileSize, stored.endTrimSeconds)

        val merged = RecentlyPlayed.withFresh(listOf(stored), listOf(t(8, 2731107L, 144.0)))[0]
        val mergedKey = StreamCache.musicCacheKey(u, merged.fileSize, merged.endTrimSeconds)
        assertNotEquals(storedKey, mergedKey)

        val recut = RecentlyPlayed.withFresh(listOf(stored), listOf(t(8, 2731107L, 140.0)))[0]
        val recutKey = StreamCache.musicCacheKey(u, recut.fileSize, recut.endTrimSeconds)
        assertNotEquals(mergedKey, recutKey)
        assertNotEquals(storedKey, recutKey)
    }
}
