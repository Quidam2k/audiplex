package com.audiplex.app.playback

import org.junit.Assert.*
import org.junit.Test

/** #5131: verifies sleep bed gain, legacy levels, migration, and ramps. */
class SleepBedGainTest {

    /** #5131: the slider midpoint preserves the old maximum without boost. */
    @Test
    fun `old maximum lands at the middle`() {
        val gain = SleepBedRules.bedGain(0.5f)

        assertEquals(0.5f, SleepBedRules.UNITY_LEVEL, 1e-4f)
        assertEquals(1f, gain.volume, 1e-4f)
        assertEquals(0f, gain.boostDb, 1e-4f)
    }

    /** #5131: endpoints and quarter level map to the expected player gain. */
    @Test
    fun `endpoints and quarter level map correctly`() {
        val maximum = SleepBedRules.bedGain(1f)
        val silent = SleepBedRules.bedGain(0f)

        assertEquals(12f, SleepBedRules.MAX_BOOST_DB, 1e-4f)
        assertEquals(1f, maximum.volume, 1e-4f)
        assertEquals(12f, maximum.boostDb, 1e-4f)
        assertEquals(0f, silent.volume, 1e-4f)
        assertEquals(0f, silent.boostDb, 1e-4f)
        assertEquals(0.5f, SleepBedRules.bedGain(0.25f).volume, 1e-4f)
    }

    /** #5131: boost increases linearly in decibels above unity. */
    @Test
    fun `three quarter level has six decibels of boost`() {
        val gain = SleepBedRules.bedGain(0.75f)

        assertEquals(1f, gain.volume, 1e-4f)
        assertEquals(6f, gain.boostDb, 1e-4f)
    }

    /** #5131: invalid slider levels clamp to the appropriate endpoint. */
    @Test
    fun `gain clamps out of range levels and treats NaN as zero`() {
        val inputs = listOf(-1f, 2f, Float.NaN)
        val endpoints = listOf(0f, 1f, 0f)

        for (index in inputs.indices) {
            val expected = SleepBedRules.bedGain(endpoints[index])
            val actual = SleepBedRules.bedGain(inputs[index])

            assertEquals(expected.volume, actual.volume, 1e-4f)
            assertEquals(expected.boostDb, actual.boostDb, 1e-4f)
        }
    }

    /** #5131: increasing the slider never decreases volume or boost. */
    @Test
    fun `gain is monotonic and boosts only above the midpoint`() {
        var previous = SleepBedRules.bedGain(0f)

        for (step in 0..100) {
            val level = step / 100f
            val gain = SleepBedRules.bedGain(level)

            assertTrue("Volume decreased at $level", gain.volume >= previous.volume)
            assertTrue("Boost decreased at $level", gain.boostDb >= previous.boostDb)
            if (level <= 0.5f) {
                assertEquals(0f, gain.boostDb, 1e-4f)
            } else {
                assertTrue("Missing boost at $level", gain.boostDb > 0f)
            }
            previous = gain
        }
    }

    /** #5131: DJ commands retain their old player volume meaning without boost. */
    @Test
    fun `DJ levels preserve old volume and never boost`() {
        for (step in 0..100) {
            val djLevel = -0.5f + 2f * step / 100f
            val level = SleepBedRules.fromDjLevel(djLevel)
            val gain = SleepBedRules.bedGain(level)

            assertTrue(level in 0f..SleepBedRules.UNITY_LEVEL)
            assertEquals(0f, gain.boostDb, 1e-4f)
            assertEquals(djLevel.coerceIn(0f, 1f), gain.volume, 1e-4f)
        }

        assertEquals(0f, SleepBedRules.fromDjLevel(Float.NaN), 1e-4f)
    }

    /** #5131: migrating the old maximum preserves its audible gain. */
    @Test
    fun `migration preserves the old maximum loudness`() {
        val migrated = SleepBedRules.migrateStoredLevel(1f, false)

        assertNotNull(migrated)
        assertEquals(0.5f, migrated!!, 1e-4f)

        val gain = SleepBedRules.bedGain(migrated)
        assertEquals(1f, gain.volume, 1e-4f)
        assertEquals(0f, gain.boostDb, 1e-4f)
    }

    /** #5131: migration halves a saved legacy level. */
    @Test
    fun `migration converts the old default to the new scale`() {
        val migrated = SleepBedRules.migrateStoredLevel(0.9f, false)

        assertNotNull(migrated)
        assertEquals(0.45f, migrated!!, 1e-4f)
    }

    /** #5131: migration clamps saved legacy levels before rescaling them. */
    @Test
    fun `migration clamps out of range stored levels`() {
        val belowMinimum = SleepBedRules.migrateStoredLevel(-1f, false)
        val aboveMaximum = SleepBedRules.migrateStoredLevel(2f, false)

        assertNotNull(belowMinimum)
        assertNotNull(aboveMaximum)
        assertEquals(0f, belowMinimum!!, 1e-4f)
        assertEquals(0.5f, aboveMaximum!!, 1e-4f)
    }

    /** #5131: absent or previously migrated values require no write. */
    @Test
    fun `migration skips completed migration and missing storage`() {
        assertNull(SleepBedRules.migrateStoredLevel(0.5f, true))
        assertNull(SleepBedRules.migrateStoredLevel(null, false))
    }

    /** #5131: the new default preserves the old default player volume. */
    @Test
    fun `default level preserves old default loudness`() {
        val gain = SleepBedRules.bedGain(SleepBedRules.DEFAULT_VOLUME)

        assertEquals(0.45f, SleepBedRules.DEFAULT_VOLUME, 1e-4f)
        assertEquals(0.9f, gain.volume, 1e-4f)
        assertEquals(0f, gain.boostDb, 1e-4f)
    }

    /** #5131: a four step ramp excludes the start and includes the target. */
    @Test
    fun `four step ramp produces evenly spaced levels`() {
        val levels = SleepBedRules.rampLevels(0f, 1f, 4)
        val expected = listOf(0.25f, 0.5f, 0.75f, 1f)

        assertEquals(expected.size, levels.size)
        for (index in expected.indices) {
            assertEquals(expected[index], levels[index], 1e-4f)
        }
    }

    /** #5131: the standard fade uses thirty two steps over eight seconds. */
    @Test
    fun `default ramp has the configured size and increases monotonically`() {
        val levels = SleepBedRules.rampLevels(0.2f, 0.6f)

        assertEquals(8_000L, SleepBedRules.RAMP_MS)
        assertEquals(32, SleepBedRules.RAMP_STEPS)
        assertEquals(SleepBedRules.RAMP_STEPS, levels.size)

        var previous = 0.2f
        for (level in levels) {
            assertTrue(level >= previous)
            assertTrue(level in 0f..1f)
            previous = level
        }
        assertEquals(0.6f, levels.last(), 1e-4f)
    }

    /** #5131: a ramp finishes at its target with exact float equality. */
    @Test
    fun `last ramp value equals the target exactly`() {
        val target = 0.1234567f
        val levels = SleepBedRules.rampLevels(0.91f, target, 7)

        assertEquals(target, levels.last(), 1e-4f)
        assertTrue("Final value must equal the target exactly", levels.last() == target)
    }

    /** #5131: ramp outputs and final targets stay within the slider range. */
    @Test
    fun `ramp clamps each level and the final target`() {
        val upward = SleepBedRules.rampLevels(-1f, 2f, 4)
        val downward = SleepBedRules.rampLevels(2f, -1f, 4)
        val expectedUpward = listOf(0f, 0.5f, 1f, 1f)
        val expectedDownward = listOf(1f, 0.5f, 0f, 0f)

        assertEquals(4, upward.size)
        assertEquals(4, downward.size)
        for (index in 0 until 4) {
            assertEquals(expectedUpward[index], upward[index], 1e-4f)
            assertEquals(expectedDownward[index], downward[index], 1e-4f)
        }
        assertTrue(upward.last() == 1f)
        assertTrue(downward.last() == 0f)
    }

    /** #5131: nonpositive step counts still produce one final target value. */
    @Test
    fun `zero and negative steps produce one value`() {
        for (steps in listOf(0, -1)) {
            val levels = SleepBedRules.rampLevels(0f, 1f, steps)

            assertEquals(1, levels.size)
            assertEquals(1f, levels.single(), 1e-4f)
        }
    }

    /**
     * #5131 (Karen's condition): fades act on the slider level, so the enhancer
     * gain moves with them. A crossfade up to a boosted level starts at 0 dB and
     * a fade down from it reaches 0 dB before the player volume drops.
     */
    @Test
    fun `fades carry the enhancer gain both ways`() {
        val steps = 20
        val up = (0..steps).map { SleepBedRules.bedGain(SleepFade.levels(it, steps, 1f, 0f, 1f).second!!) }
        assertEquals(0f, up.first().boostDb, 1e-4f)
        assertEquals(12f, up.last().boostDb, 1e-4f)
        val down = SleepBedRules.rampLevels(1f, 0f, steps).map { SleepBedRules.bedGain(it) }
        assertEquals(0f, down.last().boostDb, 1e-4f)
        assertEquals(0f, down.last().volume, 1e-4f)
        for (g in down) if (g.volume < 1f) assertEquals(0f, g.boostDb, 1e-4f)
    }

    /** #5131: a chime (DJ bed_play, old 0..1 volume) must start at once, or a 3 s chime is faded away. */
    @Test
    fun `only boosted starts ramp in, never a DJ level`() {
        for (step in 0..100) assertFalse(SleepBedRules.needsRampIn(SleepBedRules.fromDjLevel(step / 100f)))
        assertFalse(SleepBedRules.needsRampIn(SleepBedRules.UNITY_LEVEL))
        assertTrue(SleepBedRules.needsRampIn(0.51f))
        assertTrue(SleepBedRules.needsRampIn(1f))
    }
}
