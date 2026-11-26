package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** #7044: a voice vote moves the playing channel's dial the opposite way by the same dB. */
class VoiceVote7044Test {

    private val curve = { idx: Int -> -60.0 + idx * 3.0 }  // a 3 dB-per-step platform curve

    @Test
    fun raiseLowersTheDialBySameDb() {
        val db = VoiceVote.stepDb(10, 12, curve)
        assertEquals(6.0, db, 1e-9)
        assertEquals(0.5012f, VoiceVote.compensate(1f, db), 1e-3f)
    }

    @Test
    fun lowerRaisesItBackSymmetrically() {
        val down = VoiceVote.compensate(0.8f, VoiceVote.stepDb(10, 12, curve))
        val back = VoiceVote.compensate(down, VoiceVote.stepDb(12, 10, curve))
        assertEquals(0.8f, back, 1e-4f)
    }

    @Test
    fun capsAtTheSliderTopAndFloorsAtZero() {
        assertEquals(1f, VoiceVote.compensate(1f, VoiceVote.stepDb(12, 10, curve)), 0f)
        assertEquals(0f, VoiceVote.compensate(0f, 6.0), 0f)
        assertTrue(VoiceVote.compensate(0.01f, 60.0) >= 0f)
    }

    @Test
    fun unusableCurveFallsBackToFixedStep() {
        assertEquals(2 * VoiceVote.STEP_DB, VoiceVote.stepDb(4, 6) { null }, 1e-9)
        assertEquals(2 * VoiceVote.STEP_DB, VoiceVote.stepDb(4, 6) { 0.0 }, 1e-9)  // flat curve
        assertEquals(-2 * VoiceVote.STEP_DB, VoiceVote.stepDb(6, 4) { null }, 1e-9)
        // a curve that moves the wrong way (quieter on a raise) is not a measurement
        assertEquals(2 * VoiceVote.STEP_DB, VoiceVote.stepDb(4, 6) { i -> -i.toDouble() }, 1e-9)
    }

    @Test
    fun aBurstOfPressesIsCapped() {
        assertEquals(VoiceVote.MAX_STEPS * VoiceVote.STEP_DB, VoiceVote.stepDb(0, 25) { null }, 1e-9)
    }

    @Test
    fun noStepNoChange() {
        assertEquals(0.0, VoiceVote.stepDb(7, 7, curve), 0.0)
        assertEquals(0.7f, VoiceVote.compensate(0.7f, 0.0), 0f)
    }
}
