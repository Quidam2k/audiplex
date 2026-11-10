package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** #5834: a transient focus loss fades out 500 ms, then pauses; the GAIN fades back in 500 ms. */
class FocusFadeTest {
    private val port = RecordingPort()
    private val fake get() = port.inner
    private val sched = FakeScheduler()
    private val fader = FocusFader(port, sched)
    private var beforePause = 0

    private fun fade() = fader.fadeOutAndPause { beforePause++ }

    @Test fun fadesToSilenceOver500msThenPausesOnce() {
        fade()
        assertEquals(DuckRamp.level(1, 50, 0.8f, 0f), fake.history[0], 1e-6f)
        sched.advance(480)
        assertTrue(fake.volume > 0f)
        assertEquals(0, fake.pauses)
        sched.advance(10)
        assertEquals(1, fake.pauses)
        assertEquals(1, beforePause)
        assertEquals(0f, fake.history[49], 1e-6f)  // reached silence before the pause
        sched.runAll()
        assertEquals(1, fake.pauses)
    }

    @Test fun aPausedPlayerKeepsItsLevelSoAUserPlayIsAudible() {
        fade(); sched.runAll()
        assertEquals(0.8f, fake.volume, 1e-6f)
    }

    @Test fun gainMidFadeRampsBackUpWithNoPause() {
        fade(); sched.advance(200)
        assertEquals(0.8f, fader.restoreTarget!!, 1e-6f)  // what the manager hands FocusPolicy
        fader.restore(fader.restoreTarget!!, resume = true)
        assertEquals(0, fake.plays)  // still playing
        sched.runAll()
        assertEquals(0, fake.pauses)
        assertEquals(0, beforePause)
        assertEquals(0.8f, fake.volume, 1e-6f)
    }

    @Test fun resumeAfterTheFadedPauseRisesFromSilenceToTheSavedLevel() {
        fade(); sched.runAll()
        fake.history.clear()
        var done = 0
        fader.restore(0.8f, resume = true) { done++ }
        assertEquals(DuckRamp.MIN_GAIN, fake.history[0], 1e-9f)
        assertEquals(DuckRamp.restoreLevel(1, 50, DuckRamp.MIN_GAIN, 0.8f), fake.history[1], 1e-6f)
        assertEquals(1, fake.plays)
        sched.advance(480)
        assertTrue(fake.volume < 0.8f)
        sched.advance(10)
        assertEquals(0.8f, fake.volume)
        assertEquals(51, fake.history.size)
        assertEquals(1, done)
    }

    @Test fun pauseMidRestoreSavesTheTargetAndNeverJumpsUpFirst() {
        fader.duck(0.05f); sched.runAll()
        fader.restore(0.8f, resume = false); sched.advance(100)
        val partial = fake.volume
        val mark = fake.history.size
        fade()
        assertTrue(fake.history.drop(mark).all { it <= partial })
        assertEquals(0.8f, fader.restoreTarget!!, 1e-6f)
        sched.runAll()
        assertEquals(1, fake.pauses)
        assertEquals(0.8f, fake.volume, 1e-6f)  // what the instant pause used to land on
    }

    @Test fun notPlayingPausesAtOnceWithNoFadeAndNoVolumeChange() {
        fake.playing = false
        fake.volume = 0.6f
        fade()
        assertEquals(1, fake.pauses)
        assertEquals(1, beforePause)
        assertTrue(fake.history.isEmpty())
        sched.runAll()
        assertTrue(fake.history.isEmpty())
        assertEquals(0.6f, fake.volume)
    }

    @Test fun duckMidFadeTakesOverWithNoPause() {
        fade(); sched.advance(200)
        fader.duck(0.05f)
        assertEquals(0.8f, fader.preDuckVolume!!, 1e-6f)
        sched.runAll()
        assertEquals(0, fake.pauses)
        assertEquals(0.05f, fake.volume, 1e-6f)
    }

    @Test fun abandonMidFadeLandsOnTheDialLevel() {
        fade(); sched.advance(200)
        fader.reset()
        assertEquals(0.8f, fake.volume, 1e-6f)
        sched.runAll()
        assertEquals(0, fake.pauses)
    }

    @Test fun duckedThenPausedKeepsTheDuckLevelThenResumesToTheDial() {
        fader.duck(0.05f); sched.runAll()
        fade(); sched.runAll()
        assertEquals(1, fake.pauses)
        assertEquals(0.05f, fake.volume, 1e-6f)  // as the instant pause left it
        assertEquals(0.8f, fader.restoreTarget!!, 1e-6f)
        fader.restore(fader.restoreTarget!!, resume = true)
        sched.runAll()
        assertEquals(0.8f, fake.volume)
        assertEquals(1, fake.plays)
    }

    @Test fun aNewLevelMidFadeIsWhatThePausedPlayerKeeps() {
        fade(); sched.advance(100)
        assertTrue(fader.retarget(0.5f))
        sched.runAll()
        assertEquals(1, fake.pauses)
        assertEquals(0.5f, fake.volume, 1e-6f)
    }

    @Test fun duckTimingIsUnchangedByTheFade() {
        fade(); sched.runAll()
        fader.restore(0.8f, resume = true); sched.runAll()
        fake.history.clear()
        fader.duck(0.05f)
        sched.advance(60)
        assertTrue(fake.volume > 0.05f)
        sched.advance(10)
        assertEquals(0.05f, fake.volume, 1e-6f)
        assertEquals(8, fake.history.size)
    }

    @Test fun aSecondGainMidResumeKeepsAimingAtTheDialLevel() {  // Codex P1
        fade(); sched.runAll()
        fader.restore(0.8f, resume = true); sched.advance(10)
        assertEquals(0.8f, fader.restoreTarget!!, 1e-6f)  // not the half-risen volume
        fader.restore(fader.restoreTarget!!, resume = true)
        sched.runAll()
        assertEquals(0.8f, fake.volume, 1e-6f)
        assertEquals(1, fake.plays)
    }

    @Test fun aRepeatLossKeepsTheFadeDeadline() {  // Codex P2
        fade(); sched.advance(400)
        fade()
        sched.advance(90)
        assertEquals(1, fake.pauses)  // still at 490 ms, not pushed to 890
        assertEquals(1, beforePause)
    }

    @Test fun isFadingIsTrueOnlyBeforeTheFadersOwnPause() {  // Codex P2: tells a user pause from ours
        var fadingAtPause: Boolean? = null
        fader.fadeOutAndPause { fadingAtPause = fader.isFading }
        assertTrue(fader.isFading)
        sched.runAll()
        assertEquals(false, fadingAtPause)
        assertEquals(false, fader.isFading)
    }

    @Test fun aUserPauseMidFadeLandsOnTheDialLevelAndNeverPausesAgain() {  // Codex P2
        fade(); sched.advance(200)
        fake.pause()  // the user's own pause
        fader.cancelRamp()  // what onUserPause does when isFading
        assertEquals(0.8f, fake.volume, 1e-6f)
        sched.runAll()
        assertEquals(1, fake.pauses)
        assertEquals(0, beforePause)
    }
}
