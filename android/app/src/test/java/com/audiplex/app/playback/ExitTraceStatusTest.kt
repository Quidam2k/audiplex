package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins the distinction that the 2026-08-14 crash investigation needed and did
 * not have (#3031).
 *
 * The watermark-v3 re-ship delivered all 16 retained exit records and not one
 * carried a stack trace — including the CRASH that killed the app mid-command.
 * Nothing on the server could say whether Android had simply kept no trace for
 * that record or whether our own read had thrown, because `readTrace` returned
 * the same empty string in both cases. `trace_status` is the field that makes
 * those two answers different, so the NEXT crash is diagnosable.
 */
class ExitTraceStatusTest {

    private fun exit(trace: String, status: String) = ExitRecord(
        timestamp = 1_786_723_385_727L,
        reason = "CRASH",
        description = "crash",
        status = 0,
        importance = 100,
        unexpected = true,
        trace = trace,
        traceStatus = status,
    )

    @Test
    fun `trace_status is always reported`() {
        val detail = exit("", TRACE_NONE).detail()
        assertEquals(TRACE_NONE, detail["trace_status"])
    }

    @Test
    fun `a present trace is carried and labelled`() {
        val detail = exit("java.lang.IllegalStateException\n\tat com.audiplex", TRACE_PRESENT).detail()
        assertEquals(TRACE_PRESENT, detail["trace_status"])
        assertTrue(detail.getValue("trace").startsWith("java.lang.IllegalStateException"))
    }

    @Test
    fun `an absent trace omits the trace key but still explains itself`() {
        val detail = exit("", TRACE_NONE).detail()
        assertFalse("no empty trace key", detail.containsKey("trace"))
        assertEquals(TRACE_NONE, detail["trace_status"])
    }

    @Test
    fun `a failed read is distinguishable from an absent trace`() {
        // This is the whole point: on 08-14 these two were indistinguishable.
        val unreadable = exit("", "${TRACE_ERROR_PREFIX}IOException").detail()
        val absent = exit("", TRACE_NONE).detail()
        assertEquals("${TRACE_ERROR_PREFIX}IOException", unreadable["trace_status"])
        assertEquals(TRACE_NONE, absent["trace_status"])
        assertTrue(unreadable["trace_status"] != absent["trace_status"])
    }

    @Test
    fun `signal decoding still applies alongside trace_status`() {
        val detail = ExitRecord(
            timestamp = 1L,
            reason = "SIGNALED",
            description = "SIGNALED",
            status = 9,
            importance = 400,
            unexpected = false,
            traceStatus = TRACE_NONE,
        ).detail()
        assertEquals("SIGKILL", detail["signal"])
        assertEquals(TRACE_NONE, detail["trace_status"])
    }

    @Test
    fun `trimTrace bounds both lines and characters`() {
        val long = (1..100).joinToString("\n") { "frame $it" }
        val trimmed = trimTrace(long)
        assertEquals(TRACE_MAX_LINES, trimmed.lines().size)

        val wide = "x".repeat(TRACE_MAX_CHARS + 500)
        assertTrue(trimTrace(wide).endsWith("[truncated]"))
    }
}
