package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

// #5834 characterization: pins the duck/restore behavior moved verbatim out of AudioFocusManager.

internal class FakePort(override var volume: Float = 0.8f) : FocusFader.Port {
    var playing = true
    var pauses = 0
    var plays = 0
    val history = mutableListOf<Float>()
    override val isPlaying: Boolean get() = playing
    override fun pause() { pauses++; playing = false }
    override fun play() { plays++; playing = true }
}

/** Records volume sets; the fader writes through `volume`, so wrap the port. */
internal class RecordingPort(val inner: FakePort = FakePort()) : FocusFader.Port by inner {
    override var volume: Float
        get() = inner.volume
        set(value) { inner.volume = value; inner.history.add(value) }
}

internal class FakeScheduler : FocusFader.Scheduler {
    var now = 0L
    private var seq = 0
    private val pending = mutableListOf<Triple<Long, Int, () -> Unit>>()
    override fun postDelayed(delayMs: Long, block: () -> Unit) { pending.add(Triple(now + delayMs, seq++, block)) }
    override fun cancelAll() = pending.clear()

    fun advance(ms: Long) {
        val end = now + ms
        while (true) {
            val next = pending.filter { it.first <= end }.minWithOrNull(compareBy({ it.first }, { it.second })) ?: break
            pending.remove(next)
            now = next.first
            next.third()
        }
        now = end
    }

    fun runAll() {
        var guard = 0
        while (pending.isNotEmpty() && guard++ < 10_000) advance(pending.minOf { it.first } - now)
    }
}

class FocusFaderCharacterizationTest {
    private val port = RecordingPort()
    private val fake get() = port.inner
    private val sched = FakeScheduler()
    private val fader = FocusFader(port, sched)

    private fun ducked() { fader.duck(0.05f); sched.runAll() }

    @Test fun duckRampsToTargetIn80msEightSteps() {
        fader.duck(0.05f)
        assertEquals(1, fake.history.size)  // first step is synchronous
        assertEquals(DuckRamp.level(1, 8, 0.8f, 0.05f), fake.history[0], 1e-6f)
        sched.advance(60)
        assertTrue(fake.volume > 0.05f)
        sched.advance(10)
        assertEquals(0.05f, fake.volume, 1e-6f)
        assertEquals(8, fake.history.size)
        assertEquals(0.8f, fader.preDuckVolume!!, 1e-6f)
    }

    @Test fun repeatedDuckKeepsFirstPreDuckLevel() {
        ducked(); ducked()
        assertEquals(0.8f, fader.preDuckVolume!!, 1e-6f)
    }

    @Test fun restoreAfterDuckRisesOnTheDbCurveIn500ms() {
        ducked()
        fake.history.clear()
        fader.restore(0.8f, resume = false)
        sched.runAll()
        assertEquals(50, fake.history.size)
        assertEquals(0.8f, fake.history.last())
        fake.history.zipWithNext().forEach { (a, b) -> assertTrue(b >= a) }
        fake.history.forEachIndexed { i, v -> assertEquals(DuckRamp.restoreLevel(i + 1, 50, 0.05f, 0.8f), v, 1e-6f) }
        assertNull(fader.preDuckVolume)
        assertEquals(0, fake.plays)
    }

    @Test fun restoreTakes500msOfVirtualTime() {
        ducked()
        fader.restore(0.8f, resume = false)
        sched.advance(480)
        assertTrue(fake.volume < 0.8f)
        sched.advance(10)
        assertEquals(0.8f, fake.volume, 1e-6f)
    }

    @Test fun duckMidRestoreRemembersTheTargetNotThePartialVolume() {
        ducked()
        fader.restore(0.8f, resume = false)
        sched.advance(100)
        fader.duck(0.05f)
        assertEquals(0.8f, fader.preDuckVolume!!, 1e-6f)
        sched.runAll()
        assertEquals(0.05f, fake.volume, 1e-6f)
    }

    @Test fun cancelMidRestoreLandsOnTarget() {
        ducked()
        fader.restore(0.8f, resume = false)
        sched.advance(100)
        fader.cancelRamp()
        assertEquals(0.8f, fake.volume, 1e-6f)
        val size = fake.history.size
        sched.runAll()
        assertEquals(size, fake.history.size)
    }

    @Test fun resetForgetsTheDuck() {
        ducked()
        fader.reset()
        assertNull(fader.preDuckVolume)
        assertEquals(0.05f, fake.volume, 1e-6f)
    }

    @Test fun retargetWhileDuckedMovesThePreDuckLevel() {
        ducked()
        assertTrue(fader.retarget(0.4f))
        assertEquals(0.4f, fader.preDuckVolume!!, 1e-6f)
        assertEquals(0.05f, fake.volume, 1e-6f)
    }

    @Test fun retargetMidRestoreReaimsTheRamp() {
        ducked()
        fader.restore(0.8f, resume = false)
        sched.advance(100)
        assertTrue(fader.retarget(0.4f))
        sched.runAll()
        assertEquals(0.4f, fake.volume, 1e-6f)
    }

    @Test fun retargetWhenIdleReturnsFalse() {
        assertFalse(fader.retarget(0.5f))
        assertEquals(0.8f, fake.volume, 1e-6f)
    }

    @Test fun restoreWithResumePlaysOnceWhenPaused() {
        fake.playing = false
        fader.restore(0.8f, resume = true)
        assertEquals(1, fake.plays)
        fader.restore(0.8f, resume = true)
        assertEquals(1, fake.plays)
    }

    @Test fun restoreWithoutResumeNeverPlays() {
        fake.playing = false
        fader.restore(0.8f, resume = false)
        assertEquals(0, fake.plays)
    }
}
