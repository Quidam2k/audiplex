package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class OverlaySessionTest {

    private class FakePort : OverlaySession.Port {
        var view = OverlayRules.View("1705", 0L, true, false)
        var duckResult = true
        var throwOnPlay = false
        val calls = mutableListOf<String>()
        val logs = mutableListOf<Pair<String, Map<String, String>>>()
        var finished = 0

        override fun view() = view
        override fun duckMusic(fraction: Float): Boolean {
            calls += "duck"
            return duckResult
        }
        override fun restoreMusic() { calls += "restore" }
        override fun playClip() {
            calls += "play"
            if (throwOnPlay) throw IllegalStateException("boom")
        }
        override fun stopClip() { calls += "stop" }
        override fun log(event: String, detail: Map<String, String>) { logs += event to detail }
        override fun onFinished() { finished++ }
    }

    private class FakeScheduler : OverlaySession.Scheduler {
        val pending = mutableListOf<Pair<Long, () -> Unit>>()
        override fun postDelayed(delayMs: Long, block: () -> Unit) { pending += delayMs to block }
        override fun cancelAll() { pending.clear() }
        fun runAll() {
            val copy = pending.toList()
            copy.forEach { it.second() }
        }
    }

    private val port = FakePort()
    private val sched = FakeScheduler()

    private fun session() = OverlaySession(
        port, sched, clipId = 42L, forTrackId = 1705, startAtMs = 10_000L,
        duckTo = 0.35f, capMs = 5_000L, armedAtMs = 0L,
    )

    private fun count(name: String) = port.calls.count { it == name }

    private fun endReason() = port.logs.last { it.first == "overlay_end" }.second["reason"]

    @Test
    fun waitsBeforeStartAt() {
        val s = session()
        port.view = port.view.copy(positionMs = 5_000L)
        s.tick(100)
        assertEquals(OverlaySession.State.Armed, s.state)
        assertTrue(port.calls.isEmpty())
    }

    @Test
    fun startsAtStartAtDuckThenPlay() {
        val s = session()
        port.view = port.view.copy(positionMs = 10_000L)
        s.tick(100)
        assertEquals(OverlaySession.State.Playing, s.state)
        assertEquals(listOf("duck", "play"), port.calls)
        assertEquals(5_000L, sched.pending.single().first)
        assertEquals("overlay_start", port.logs.first().first)
    }

    @Test
    fun dropsOnTrackChangeBeforeStart() {
        val s = session()
        port.view = port.view.copy(mediaId = "999")
        s.tick(100)
        assertEquals(OverlaySession.State.Done, s.state)
        assertTrue(port.calls.isEmpty())
        assertEquals("track_changed", endReason())
        assertEquals(1, port.finished)
    }

    @Test
    fun duckedWaitsThenDropsAfterHoldCap() {
        val s = session()
        port.view = port.view.copy(positionMs = 10_000L, ducked = true)
        s.tick(1_000)
        s.tick(1_000 + DjClipRules.HOLD_CAP_MS)
        assertEquals(OverlaySession.State.Armed, s.state)
        s.tick(1_001 + DjClipRules.HOLD_CAP_MS)
        assertEquals(OverlaySession.State.Done, s.state)
        assertEquals("ducked_timeout", endReason())
        assertTrue(port.calls.isEmpty())
    }

    @Test
    fun endsNormallyWithExactlyOneRestore() {
        val s = session()
        port.view = port.view.copy(positionMs = 10_000L)
        s.tick(100)
        s.onClipEnded()
        assertEquals(OverlaySession.State.Done, s.state)
        assertEquals(1, count("restore"))
        assertEquals(1, count("stop"))
        assertEquals(1, port.finished)
        assertTrue(sched.pending.isEmpty())
    }

    @Test
    fun errorPathRestoresAndStops() {
        val s = session()
        port.view = port.view.copy(positionMs = 10_000L)
        s.tick(100)
        s.onClipError("decoder")
        assertEquals(1, count("restore"))
        assertEquals(1, count("stop"))
        assertEquals("decoder", endReason())
    }

    @Test
    fun capFiresRestores() {
        val s = session()
        port.view = port.view.copy(positionMs = 10_000L)
        s.tick(100)
        sched.runAll()
        assertEquals(OverlaySession.State.Done, s.state)
        assertEquals(1, count("restore"))
        assertEquals("capped", port.logs.last().second["outcome"])
    }

    @Test
    fun cancelWhilePlayingRestores() {
        val s = session()
        port.view = port.view.copy(positionMs = 10_000L)
        s.tick(100)
        s.cancel("new_overlay")
        assertEquals(1, count("restore"))
        assertEquals("cancelled", port.logs.last().second["outcome"])
    }

    @Test
    fun cancelWhileArmedNoRestore() {
        val s = session()
        s.cancel("release")
        assertEquals(OverlaySession.State.Done, s.state)
        assertEquals(0, count("restore"))
        assertEquals(0, count("stop"))
        assertEquals(1, port.finished)
    }

    @Test
    fun preemptedRestoresOnce() {
        val s = session()
        port.view = port.view.copy(positionMs = 10_000L)
        s.tick(100)
        s.onPreempted("persona")
        assertEquals(1, count("restore"))
        assertEquals("preempted", port.logs.last().second["outcome"])
    }

    @Test
    fun finishTwiceOneRestore() {
        val s = session()
        port.view = port.view.copy(positionMs = 10_000L)
        s.tick(100)
        s.onClipEnded()
        s.onClipEnded()
        s.cancel("late")
        sched.runAll()
        assertEquals(1, count("restore"))
        assertEquals(1, port.finished)
        assertEquals(1, port.logs.count { it.first == "overlay_end" })
    }

    @Test
    fun duckRefusedNoPlayNoRestore() {
        val s = session()
        port.duckResult = false
        port.view = port.view.copy(positionMs = 10_000L)
        s.tick(100)
        assertEquals(OverlaySession.State.Done, s.state)
        assertEquals(0, count("play"))
        assertEquals(0, count("restore"))
        assertEquals("duck_refused", endReason())
    }

    @Test
    fun playClipThrowsRestoresOnce() {
        val s = session()
        port.throwOnPlay = true
        port.view = port.view.copy(positionMs = 10_000L)
        s.tick(100)
        assertEquals(OverlaySession.State.Done, s.state)
        assertEquals(1, count("restore"))
        assertEquals("error", port.logs.last().second["outcome"])
        assertEquals(1, port.finished)
    }

    @Test
    fun neverReachedAfterTenMinutes() {
        val s = session()
        port.view = port.view.copy(positionMs = 0L)
        s.tick(OverlayRules.MAX_SCHEDULE_MS)
        assertEquals(OverlaySession.State.Armed, s.state)
        s.tick(OverlayRules.MAX_SCHEDULE_MS + 1)
        assertEquals(OverlaySession.State.Done, s.state)
        assertEquals("never_reached", endReason())
        assertTrue(port.calls.isEmpty())
    }

    @Test
    fun restoreCapMsRules() {
        assertEquals(5_500L, OverlayRules.restoreCapMs(3.5))
        assertEquals(20_000L, OverlayRules.restoreCapMs(null))
        assertEquals(20_000L, OverlayRules.restoreCapMs(0.0))
        assertEquals(20_000L, OverlayRules.restoreCapMs(-4.0))
        assertEquals(20_000L, OverlayRules.restoreCapMs(Double.NaN))
        assertEquals(20_000L, OverlayRules.restoreCapMs(Double.POSITIVE_INFINITY))
        assertEquals(60_000L, OverlayRules.restoreCapMs(1_000.0))
        assertEquals(60_000L, OverlayRules.restoreCapMs(1e300))
    }

    @Test
    fun duckToClamps() {
        assertEquals(0.35f, OverlayRules.duckTo(null), 0f)
        assertEquals(0.05f, OverlayRules.duckTo(0f), 0f)
        assertEquals(0.05f, OverlayRules.duckTo(-1f), 0f)
        assertEquals(1f, OverlayRules.duckTo(5f), 0f)
        assertEquals(0.5f, OverlayRules.duckTo(0.5f), 0f)
        assertEquals(0.35f, OverlayRules.duckTo(Float.NaN), 0f)
    }
}
