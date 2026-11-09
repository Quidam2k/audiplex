package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** #5684: swipe-remove keeps the playing song playing; a drag lands on the row it passed. */
class QueueMathTest {
    @Test fun removingBeforeTheCurrentShiftsItDown() {
        assertEquals(listOf("a", "c", "d") to 1, queueWithout(listOf("a", "b", "c", "d"), 2, 1))
        assertEquals(listOf("a", "b", "c") to 2, queueWithout(listOf("a", "b", "c", "d"), 2, 3))
    }

    @Test fun thePlayingSongAndBadIndexesAreNeverRemoved() {
        assertNull(queueWithout(listOf("a", "b"), 1, 1))
        assertNull(queueWithout(listOf("a", "b"), 0, 5))
    }

    @Test fun dragTargetRoundsAndClamps() {
        assertEquals(3, dragTarget(1, 130f, 64f, 6))
        assertEquals(1, dragTarget(1, 20f, 64f, 6))
        assertEquals(0, dragTarget(1, -500f, 64f, 6))
        assertEquals(5, dragTarget(4, 500f, 64f, 6))
    }
}
