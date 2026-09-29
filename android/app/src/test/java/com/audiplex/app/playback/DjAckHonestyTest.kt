package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The ack means playback STARTED, the now-playing report can't go stale, and a
 * DJ mix never touches the current song (#2843 / #2842).
 *
 * On 2026-09-27 the phone acked two play_now commands "ok" and played nothing,
 * and now-playing read "never" — the DJ had no way to know. Each of those
 * shapes is pinned here.
 */
class DjAckHonestyTest {

    private val ok = DispatchResult("ok")

    @Test
    fun `playing after load keeps the base result`() {
        assertEquals(ok, startResult(started = true, error = null, base = ok))
    }

    @Test
    fun `partial resolution detail survives a real start`() {
        val partial = partialOrOk(listOf(1, 2, 3), listOf(1, 2))
        assertEquals(partial, startResult(started = true, error = null, base = partial))
    }

    @Test
    fun `silent after load is a failure, not ok`() {
        val result = startResult(started = false, error = null, base = ok)
        assertEquals("failed", result.status)
        assertEquals("not playing 8s after load", result.detail)
    }

    @Test
    fun `player error wins and carries its reason`() {
        val result = startResult(started = false, error = "Source error", base = ok)
        assertEquals("failed", result.status)
        assertEquals("player error: Source error", result.detail)
    }

    @Test
    fun `idle state still reports on the heartbeat`() {
        assertEquals(false, shouldReport(false, "k", "k", sinceLastMs = 59_999))
        assertEquals(true, shouldReport(false, "k", "k", sinceLastMs = 60_000))
        // A fresh client (lastReportAt = 0) reports on its first tick.
        assertEquals(true, shouldReport(false, "k", "", sinceLastMs = 5_000))
        assertEquals(true, shouldReport(true, "k", "k", sinceLastMs = 0))
    }

    @Test
    fun `replacing the tail never touches the current song or what played`() {
        val queue = listOf("played", "CURRENT", "old1", "old2")
        assertEquals(
            listOf("played", "CURRENT", "new1", "new2"),
            replaceTail(queue, currentIndex = 1, newTail = listOf("new1", "new2")),
        )
    }

    @Test
    fun `an empty tail just trims after the current song`() {
        assertEquals(
            listOf("CURRENT"),
            replaceTail(listOf("CURRENT", "old"), currentIndex = 0, newTail = emptyList()),
        )
    }

    @Test
    fun `current song at the end of the queue gets the tail appended`() {
        assertEquals(
            listOf("a", "CURRENT", "new"),
            replaceTail(listOf("a", "CURRENT"), currentIndex = 1, newTail = listOf("new")),
        )
    }

    @Test
    fun `seamless swap over a playing song is ok but says unproven`() {
        val result = startResult(false, null, ok, wasPlaying = true, stillPlaying = true)
        assertEquals("ok", result.status)
        assertEquals("start not confirmed: audio never paused during the swap", result.detail)
    }

    @Test
    fun `swap that stopped and never resumed is a failure`() {
        val result = startResult(false, null, ok, wasPlaying = true, stillPlaying = false)
        assertEquals("failed", result.status)
    }

    @Test
    fun `redelivery replays the original outcome, marked as a replay`() {
        val replay = replayResult(DispatchResult("ok", "resolved 2 of 3 track ids"), deliveryCount = 2)
        assertEquals("ok", replay.status)
        assertEquals("redelivery 2, already executed; resolved 2 of 3 track ids", replay.detail)
        assertEquals("no_tracks", replayResult(DispatchResult("no_tracks"), 2).status)
    }

    @Test
    fun `redelivery with no record is never a bare ok`() {
        assertEquals("duplicate", replayResult(null, 3).status)
    }
}
