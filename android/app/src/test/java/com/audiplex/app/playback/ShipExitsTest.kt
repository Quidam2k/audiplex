package com.audiplex.app.playback

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
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

/**
 * Pins the three decoding defects fixed in #3021, all of which made a real
 * process death land in the log saying nothing useful.
 */
class ExitDecodingTest {

    @Test
    fun `blank description falls back to the reason name`() {
        // The observed SIGNALED records carry "" — not null — which is exactly
        // what the original null check let through.
        assertEquals("SIGNALED", resolveDescription("", "SIGNALED"))
        assertEquals("SIGNALED", resolveDescription("   ", "SIGNALED"))
        assertEquals("SIGNALED", resolveDescription(null, "SIGNALED"))
        assertEquals("Java heap space", resolveDescription("Java heap space", "CRASH"))
    }

    @Test
    fun `signal status decodes to a name`() {
        assertEquals("SIGKILL", signalName(9))
        assertEquals("SIGABRT", signalName(6))
        assertEquals("SIGSEGV", signalName(11))
        // status 0 is "no signal" — must not be labelled as one.
        assertEquals(null, signalName(0))
        assertEquals(null, signalName(99))
    }

    @Test
    fun `trace is capped at the line limit`() {
        val raw = (1..200).joinToString("\n") { "frame $it" }
        val trimmed = trimTrace(raw)
        assertEquals(TRACE_MAX_LINES, trimmed.lines().size)
        assertEquals("frame 1", trimmed.lines().first())
        assertEquals("frame $TRACE_MAX_LINES", trimmed.lines().last())
    }

    @Test
    fun `trace is capped in characters even when the line count is sane`() {
        // One pathological line — a giant ANR dump header — must not blow the
        // payload just because it counts as a single line.
        val trimmed = trimTrace("x".repeat(TRACE_MAX_CHARS * 2))
        assertTrue(trimmed.length <= TRACE_MAX_CHARS + "\n[truncated]".length)
        assertTrue(trimmed.endsWith("[truncated]"))
    }

    @Test
    fun `blank trace is dropped rather than shipped empty`() {
        assertEquals("", trimTrace(""))
        assertEquals("", trimTrace("   "))
    }

    @Test
    fun `detail carries signal and trace only when they mean something`() {
        val killed = ExitRecord(1L, "SIGNALED", "SIGNALED", 9, 400, true, trace = "")
        assertEquals("SIGKILL", killed.detail()["signal"])
        assertEquals(null, killed.detail()["trace"])

        val crashed = ExitRecord(2L, "CRASH", "boom", 0, 100, true, trace = "at Foo.bar()")
        assertEquals(null, crashed.detail()["signal"])
        assertEquals("at Foo.bar()", crashed.detail()["trace"])
    }
}
