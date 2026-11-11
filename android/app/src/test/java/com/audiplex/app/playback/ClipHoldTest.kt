package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

// #5837 F: a DJ clip never starts under a persona line; it waits for the GAIN, at most 20 s.
class ClipHoldTest {
    private class Port : ClipHold.Port {
        override var ducked = true
        override var playWhenReady = true
        var ours = false
        var pauses = 0
        var plays = 0
        val logs = mutableListOf<String>()
        override fun markOurPause() { ours = true }
        override fun clearOurPause() { ours = false }
        override fun pause() { pauses++; playWhenReady = false }
        override fun play() { plays++; playWhenReady = true }
        override fun log(message: String) { logs.add(message) }
    }

    private val port = Port()
    private val sched = FakeScheduler()
    private val hold = ClipHold(port, sched)
    private val clip = DjClipRules.clipMediaId(42L)

    @Test fun aClipStartingWhileDuckedIsHeldAsOurPause() {
        assertTrue(hold.onItemStart(clip))
        assertTrue(hold.held)
        assertEquals(1, port.pauses)
        assertTrue(port.ours)  // so onUserPause keeps focus and the GAIN resumes it
    }

    @Test fun aSongOrAnUnduckedClipIsNotHeld() {
        assertFalse(hold.onItemStart("song"))
        port.ducked = false
        assertFalse(hold.onItemStart(clip))
        assertEquals(0, port.pauses)
    }

    @Test fun theGainsResumeReleasesItWithoutASecondPlay() {
        hold.onItemStart(clip)
        port.play()  // the GAIN's restore plays the player...
        hold.onPlayRequested()  // ...and the play ends the hold
        assertFalse(hold.held)
        assertEquals(1, port.plays)
        sched.advance(DjClipRules.HOLD_CAP_MS)
        assertEquals(1, port.plays)  // the cap was cancelled
    }

    @Test fun theCapPlaysItAfter20sAndForgetsTheFocusPause() {
        hold.onItemStart(clip)
        sched.advance(DjClipRules.HOLD_CAP_MS - 1)
        assertEquals(0, port.plays)
        sched.advance(1)
        assertEquals(1, port.plays)
        assertFalse(port.ours)  // a later GAIN must not resume anything
        assertFalse(hold.held)
    }

    @Test fun oneHoldAtATime() {
        hold.onItemStart(clip)
        port.playWhenReady = true
        assertFalse(hold.onItemStart(DjClipRules.clipMediaId(43L)))
        assertEquals(1, port.pauses)
    }

    @Test fun resetDropsTheCap() {
        hold.onItemStart(clip)
        hold.reset()
        sched.advance(DjClipRules.HOLD_CAP_MS)
        assertEquals(0, port.plays)
        assertFalse(hold.held)
    }
}
