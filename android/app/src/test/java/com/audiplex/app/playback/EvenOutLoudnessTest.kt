package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** #5626: the Even out with our voices switch (Todd 56733). */
class EvenOutLoudnessTest {

    @Test
    fun switchOffPreservesServerSettings() {
        val (target, maxBoost) = MixLevels.musicTarget(false, -14.0, 6.0)

        assertEquals(-14.0, target, 1e-9)
        assertEquals(6.0, maxBoost, 1e-9)
        assertEquals(4.5, MixLevels.bookGainDb(false, 4.5, -18.5)!!, 1e-9)
        assertNull(MixLevels.bookGainDb(false, null, null))
    }

    @Test
    fun switchOnLowersMusicTargetAndDisablesBoost() {
        val (target, maxBoost) = MixLevels.musicTarget(true, -14.0, 6.0)
        val (lowerTarget, lowerMaxBoost) = MixLevels.musicTarget(true, -23.0, 6.0)

        assertEquals(-20.0, target, 1e-9)
        assertEquals(0.0, maxBoost, 1e-9)
        assertEquals(-23.0, lowerTarget, 1e-9)
        assertEquals(0.0, lowerMaxBoost, 1e-9)
    }

    @Test
    fun switchControlsMusicGain() {
        val (onTarget, onMaxBoost) = MixLevels.musicTarget(true, -14.0, 6.0)
        val (offTarget, offMaxBoost) = MixLevels.musicTarget(false, -14.0, 6.0)

        assertEquals(0.0, LoudnessGain.gainDbFor(-24.0, onTarget, null, onMaxBoost), 1e-9)
        assertEquals(-11.0, LoudnessGain.gainDbFor(-9.0, onTarget, null, onMaxBoost), 1e-9)
        assertEquals(6.0, LoudnessGain.gainDbFor(-24.0, offTarget, null, offMaxBoost), 1e-9)
        assertEquals(-5.0, LoudnessGain.gainDbFor(-9.0, offTarget, null, offMaxBoost), 1e-9)

        assertEquals(0.0, LoudnessGain.gainDbFor(-14.0, offTarget, null, offMaxBoost), 1e-9)
        assertEquals(-6.0, LoudnessGain.gainDbFor(-14.0, onTarget, null, onMaxBoost), 1e-9)
    }

    @Test
    fun switchOnAttenuatesBooksWithoutRaisingThem() {
        assertEquals(-4.0, MixLevels.bookGainDb(true, 2.0, -16.0)!!, 1e-9)
        assertEquals(0.0, MixLevels.bookGainDb(true, 6.0, -26.0)!!, 1e-9)
    }

    @Test
    fun switchOnHandlesUnmeasuredBooks() {
        assertEquals(0.0, MixLevels.bookGainDb(true, 3.0, null)!!, 1e-9)
        assertEquals(-2.0, MixLevels.bookGainDb(true, -2.0, null)!!, 1e-9)
        assertNull(MixLevels.bookGainDb(true, null, null))
        assertEquals(-1.0, MixLevels.bookGainDb(true, -1.0, Double.NaN)!!, 1e-9)
    }

    @Test
    fun switchOverridesRaisesOff() {
        assertTrue(MixLevels.raisesOff(null, true)!!)
        assertTrue(MixLevels.raisesOff("boost_needs_limiter", true)!!)
        assertEquals(false, MixLevels.raisesOff("boost_needs_limiter", false))
        assertNull(MixLevels.raisesOff(null, false))
        assertTrue(MixLevels.raisesOff("attenuate_only", false)!!)
    }

    @Test
    fun bedtimeShiftRoundTripFollowsSwitch() {
        val on = MixLevels.raisesOff("boost_needs_limiter", true)!!
        val shift = MixLevels.bedtimeShift(0.5f, null, on)!!

        assertEquals(0.99763116f, shift.level, 1e-3f)
        assertEquals(6f, shift.appliedDb!!, 1e-3f)
        assertNull(MixLevels.bedtimeShift(shift.level, shift.appliedDb, on))

        val off = MixLevels.raisesOff("boost_needs_limiter", false)!!
        val restored = MixLevels.bedtimeShift(shift.level, shift.appliedDb, off)!!

        assertEquals(0.5f, restored.level, 1e-3f)
        assertNull(restored.appliedDb)
    }
}
