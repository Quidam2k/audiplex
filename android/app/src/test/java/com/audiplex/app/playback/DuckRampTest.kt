package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pantheon #3597: the Talk-ON duck ramps instead of snapping ("a right angle"). */
class DuckRampTest {
    @Test
    fun `duck and restore take hundreds of ms in many small steps`() {
        assertEquals(16, DuckRamp.steps(DuckRamp.DUCK_MS))
        assertEquals(24, DuckRamp.steps(DuckRamp.RESTORE_MS))
        assertTrue(DuckRamp.DUCK_MS in 300L..800L && DuckRamp.RESTORE_MS in 300L..800L)
        assertEquals(1, DuckRamp.steps(0L))
    }

    @Test
    fun `levels run linearly from start to target and land exactly`() {
        val steps = DuckRamp.steps(DuckRamp.DUCK_MS)
        assertEquals(0.65f, DuckRamp.level(0, steps, 0.65f, 0.05f), 1e-6f)
        assertEquals(0.35f, DuckRamp.level(steps / 2, steps, 0.65f, 0.05f), 1e-6f)
        assertEquals(0.05f, DuckRamp.level(steps, steps, 0.65f, 0.05f), 1e-6f)
        // no single step is a cliff: max jump <= 1/steps of the span
        val jumps = (1..steps).map { DuckRamp.level(it, steps, 0.65f, 0.05f) - DuckRamp.level(it - 1, steps, 0.65f, 0.05f) }
        assertTrue(jumps.all { kotlin.math.abs(it) <= 0.6f / steps + 1e-6f })
    }

    @Test
    fun `levels clamp to the player range`() {
        assertEquals(1f, DuckRamp.level(5, 4, 0.5f, 1.5f), 1e-6f)
        assertEquals(0f, DuckRamp.level(4, 4, 0.5f, -1f), 1e-6f)
    }
}
