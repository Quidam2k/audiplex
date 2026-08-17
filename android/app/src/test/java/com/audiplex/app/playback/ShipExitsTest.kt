package com.audiplex.app.playback

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Pins the one invariant that made the first cut of process-exit reporting
 * worse than useless (#2961): the watermark must never advance past a report
 * the server did not take.
 *
 * That version reported into a null API client at startup, dropped every
 * entry, and marked them all as done anyway — silently consuming the process
 * deaths it existed to capture, permanently. Report-then-mark-done-without-
 * delivery is the failure shape; these tests exist so it cannot come back.
 */
class ShipExitsTest {

    private fun exit(timestamp: Long) = ExitRecord(
        timestamp = timestamp,
        reason = "CRASH",
        description = "died",
        status = 0,
        importance = 100,
        unexpected = true,
    )

    @Test
    fun `watermark advances to the last delivered entry`() = runTest {
        val sent = mutableListOf<Long>()
        val result = shipExits(listOf(exit(100), exit(200), exit(300)), since = 0L) {
            sent += it.timestamp
            true
        }
        assertEquals(listOf(100L, 200L, 300L), sent)
        assertEquals(300L, result)
    }

    @Test
    fun `watermark does not advance past a failed post`() = runTest {
        val sent = mutableListOf<Long>()
        val result = shipExits(listOf(exit(100), exit(200), exit(300)), since = 0L) {
            sent += it.timestamp
            it.timestamp != 200L
        }
        // 300 must NOT be attempted-and-marked behind a hole at 200.
        assertEquals(listOf(100L, 200L), sent)
        assertEquals(100L, result)
    }

    @Test
    fun `watermark stays put when nothing can be delivered`() = runTest {
        // The exact shipped bug: no API client, every send fails. Previously
        // this still advanced the watermark and lost the records forever.
        val result = shipExits(listOf(exit(100), exit(200)), since = 0L) { false }
        assertEquals(0L, result)
    }

    @Test
    fun `already reported entries are not resent`() = runTest {
        val sent = mutableListOf<Long>()
        val result = shipExits(listOf(exit(100), exit(200), exit(300)), since = 200L) {
            sent += it.timestamp
            true
        }
        assertEquals(listOf(300L), sent)
        assertEquals(300L, result)
    }

    @Test
    fun `entries ship oldest first regardless of input order`() = runTest {
        val sent = mutableListOf<Long>()
        shipExits(listOf(exit(300), exit(100), exit(200)), since = 0L) {
            sent += it.timestamp
            true
        }
        assertEquals(listOf(100L, 200L, 300L), sent)
    }

    @Test
    fun `no entries means no watermark movement`() = runTest {
        assertEquals(0L, shipExits(emptyList(), since = 0L) { true })
    }
}
