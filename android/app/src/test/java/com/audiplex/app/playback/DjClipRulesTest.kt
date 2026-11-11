package com.audiplex.app.playback

import com.audiplex.app.playback.DjClipRules.Edge
import com.audiplex.app.playback.DjClipRules.PauseAfter
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

// #5836 step 3 (announce then="pause") and #5837 F (hold a DJ clip under another app's duck).
class DjClipRulesTest {
    private val clip = DjClipRules.clipMediaId(1731282294000L)
    private val armed = PauseAfter(clip, 3_000L)

    @Test fun clipIdsAreDjClips() {
        assertEquals("djclip:1731282294000", clip)
        assertTrue(DjClipRules.isDjClip(clip))
        assertFalse(DjClipRules.isDjClip("track:5"))
        assertFalse(DjClipRules.isDjClip(null))
    }

    @Test fun enteringTheArmedClipArmsPauseAtItsEnd() {
        assertEquals(Edge.ArmPauseAtEnd, DjClipRules.onTransition("song", clip, auto = true, armed, pauseAtEndSet = false))
        assertEquals(armed, DjClipRules.armedAfter("song", clip, armed))  // still armed while the clip plays
    }

    @Test fun nothingArmedNothingHappens() {
        assertEquals(Edge.None, DjClipRules.onTransition("song", clip, auto = true, armed = null, pauseAtEndSet = false))
        assertEquals(Edge.None, DjClipRules.onTransition("a", "b", auto = true, armed, pauseAtEndSet = true))
    }

    @Test fun rollingIntoTheNextSongAnywayFadesOutThenPauses() {
        // The stop at the clip's end did not happen (flag still set) and the player advanced on its own.
        assertEquals(Edge.FadeAndPause(3_000L), DjClipRules.onTransition(clip, "next", auto = true, armed, pauseAtEndSet = true))
        assertNull(DjClipRules.armedAfter(clip, "next", armed))
    }

    @Test fun skippingOffTheClipDropsTheStopSoTheNextSongIsNotCut() {
        assertEquals(Edge.CancelPauseAtEnd, DjClipRules.onTransition(clip, "next", auto = false, armed, pauseAtEndSet = true))
        assertNull(DjClipRules.armedAfter(clip, "next", armed))
    }

    @Test fun aPlayAfterTheStopNeverPausesAgain() {
        // The end-of-item pause fired and cleared the flag; Todd pressed play: the advance is his.
        assertEquals(Edge.None, DjClipRules.onTransition(clip, "next", auto = true, armed, pauseAtEndSet = false))
        assertNull(DjClipRules.armedAfter(clip, "next", armed))
    }

    @Test fun holdOnlyADjClipStartingWhileDuckedAndPlaying() {
        assertTrue(DjClipRules.shouldHold(clip, ducked = true, playWhenReady = true))
        assertFalse(DjClipRules.shouldHold(clip, ducked = false, playWhenReady = true))
        assertFalse(DjClipRules.shouldHold(clip, ducked = true, playWhenReady = false))
        assertFalse(DjClipRules.shouldHold("song", ducked = true, playWhenReady = true))
        assertEquals(20_000L, DjClipRules.HOLD_CAP_MS)
    }

    @Test fun fadeMsDefaultsAndClamps() {
        assertEquals(3_000L, DjClipRules.fadeMs(null))
        assertEquals(1_500L, DjClipRules.fadeMs(1_500L))
        assertEquals(0L, DjClipRules.fadeMs(-5L))
        assertEquals(10_000L, DjClipRules.fadeMs(60_000L))
    }

    @Test fun theOutroStopFadesOverItsOwnDurationThenPauses() {
        val port = RecordingPort()
        val sched = FakeScheduler()
        val fader = FocusFader(port, sched)
        var noted = false
        fader.fadeOutAndPause(3_000L) { noted = true }
        sched.advance(2_900L)
        assertEquals(0, port.inner.pauses)  // still fading at 2.9 s
        assertTrue(port.inner.volume > 0f)
        sched.advance(200L)
        assertEquals(1, port.inner.pauses)
        assertTrue(noted)
        assertEquals(0.8f, port.inner.volume, 1e-4f)  // left at the dial level, so a later play is not silent
    }

    @Test fun theFocusPauseKeepsItsDefault500ms() {
        val port = RecordingPort()
        val sched = FakeScheduler()
        FocusFader(port, sched).fadeOutAndPause { }
        sched.advance(DuckRamp.PAUSE_FADE_MS)
        assertEquals(1, port.inner.pauses)
    }
}
