package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** #4217: the byte ranges a book resume prefetches, the book cache cap and the cache routing. */
class BookIndexPrefetchTest {

    @Test fun headAndMoovAtEnd() {
        val size = 100_000_000L
        val ranges = indexRanges(95_000_000L, 5_000_000L, size)
        assertEquals(listOf(0L until HEAD_BYTES, 95_000_000L until size), ranges)
    }

    @Test fun moovInsideHeadMergesIntoOneRange() {
        assertEquals(listOf(0L until HEAD_BYTES), indexRanges(100L, 2_000L, 10_000_000L))
        assertEquals(listOf(0L until 200_000L), indexRanges(1_000L, 199_000L, 10_000_000L))
    }

    @Test fun unknownOrBadRangesPrefetchNothing() {
        assertTrue(indexRanges(null, null, 1_000_000L).isEmpty())
        assertTrue(indexRanges(10L, 0L, 1_000_000L).isEmpty())
        assertTrue(indexRanges(999_000L, 5_000L, 1_000_000L).isEmpty())  // runs past EOF: stale size
        assertTrue(indexRanges(-1L, 5L, 1_000_000L).isEmpty())
    }

    @Test fun smallFileHeadStopsAtEof() {
        assertEquals(listOf(0L until 10_000L), indexRanges(9_000L, 1_000L, 10_000L))
    }

    @Test fun bookCacheDropsTo512MbWhenSpaceIsTight() {
        val gb = 1024L * 1024 * 1024
        assertEquals(512L * 1024 * 1024, StreamCache.bookCapBytes(3 * gb))
        assertEquals(gb, StreamCache.bookCapBytes(20 * gb))
    }

    @Test fun onlyBookStreamsUseTheBookCache() {
        assertTrue(StreamCache.isBookPath("/api/stream/12"))
        assertFalse(StreamCache.isBookPath("/api/music/stream/track/7"))
        assertFalse(StreamCache.isBookPath(null))
    }

    @Test fun cacheKeyMatchesPlayback() {
        assertEquals("http://x/api/stream/3@42", bookCacheKey("http://x/api/stream/3", 42L))
    }
}
