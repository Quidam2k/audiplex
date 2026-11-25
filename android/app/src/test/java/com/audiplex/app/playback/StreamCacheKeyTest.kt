package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

/** #6995/#5993: music cache keys carry file size + end trim, so a re-cut track is fetched fresh. */
class StreamCacheKeyTest {

    private val u = "http://h:8100/api/music/stream/track/8"

    @Test fun sameInputsGiveEqualKey() {
        assertEquals(
            StreamCache.musicCacheKey(u, 2731107L, 151.299),
            StreamCache.musicCacheKey(u, 2731107L, 151.299)
        )
    }

    @Test fun differentTrimGivesDifferentKey() {
        assertNotEquals(
            StreamCache.musicCacheKey(u, 2731107L, 151.299),
            StreamCache.musicCacheKey(u, 2731107L, 144.0)
        )
    }

    @Test fun differentFileSizeGivesDifferentKey() {
        assertNotEquals(
            StreamCache.musicCacheKey(u, 2731107L, 151.299),
            StreamCache.musicCacheKey(u, 2731108L, 151.299)
        )
    }

    @Test fun nullTrimVersusTrimGivesDifferentKey() {
        assertNotEquals(
            StreamCache.musicCacheKey(u, 2731107L, null),
            StreamCache.musicCacheKey(u, 2731107L, 144.0)
        )
    }

    @Test fun keyNeverEqualsBareUrl() {
        assertNotEquals(u, StreamCache.musicCacheKey(u, null, null))
        assertNotEquals(u, StreamCache.musicCacheKey(u, 2731107L, null))
        assertNotEquals(u, StreamCache.musicCacheKey(u, 2731107L, 144.0))
    }

    @Test fun nullFileSizeVersusSizeGivesDifferentKey() {
        assertNotEquals(
            StreamCache.musicCacheKey(u, null, 144.0),
            StreamCache.musicCacheKey(u, 2731107L, 144.0)
        )
    }

    // A real SimpleCache round-trip can't run here: its constructor blocks on
    // android.os.ConditionVariable, which plain JVM unit tests stub out. Media3 uses
    // MediaItem.customCacheKey as the DataSpec key, so a new key is a cache miss.
}
