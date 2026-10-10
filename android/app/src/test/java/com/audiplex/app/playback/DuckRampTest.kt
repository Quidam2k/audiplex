package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pantheon #3597: the Talk-ON duck ramps instead of snapping ("a right angle"). */
class DuckRampTest {
    @Test
    fun `duck and restore take hundreds of ms in many small steps`() {
        assertEquals(8, DuckRamp.steps(DuckRamp.DUCK_MS))  // Pantheon #7622: 80 ms in 10 ms steps
        assertEquals(50, DuckRamp.steps(DuckRamp.RESTORE_MS))  // Pantheon #2187: 500 ms
        assertTrue(DuckRamp.DUCK_MS in 60L..150L && DuckRamp.RESTORE_MS in 300L..800L)  // Pantheon #7622
        // Pantheon #7622: Pantheon's phone opens the BT mic 120 ms after Talk-ON; the duck must be done by then
        assertTrue(DuckRamp.DUCK_MS <= 120L)
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

    /** Pantheon #2187: Talk-OFF un-duck "blasts my ears". The restore curve must never overshoot. */
    @Test
    fun `restore curve starts at the duck, rises monotonically, never exceeds target, lands exactly`() {
        val steps = DuckRamp.steps(DuckRamp.RESTORE_MS)
        for (target in listOf(1f, 0.6f, 0.3f)) {
            val xs = (0..steps).map { DuckRamp.restoreLevel(it, steps, 0.05f, target) }
            assertEquals(0.05f, xs.first(), 1e-6f)
            assertEquals(target, xs.last(), 1e-6f)
            assertTrue("never above target", xs.all { it <= target + 1e-6f })
            assertTrue("monotonic", xs.zipWithNext().all { (a, b) -> b >= a - 1e-6f })
        }
    }

    @Test
    fun `restore curve is gentle early - equal dB steps, not an amplitude jump`() {
        val steps = DuckRamp.steps(DuckRamp.RESTORE_MS)
        val third = DuckRamp.restoreLevel(steps / 3, steps, 0.05f, 1f)
        val linearThird = DuckRamp.level(steps / 3, steps, 0.05f, 1f)
        assertTrue("a third of the way is well under linear ($third vs $linearThird)", third < linearThird / 2)
        // every step is the same size in dB (+-0.01 dB)
        val db = (0..steps).map { 20 * kotlin.math.log10(DuckRamp.restoreLevel(it, steps, 0.05f, 1f).toDouble()) }
        val d = db.zipWithNext().map { (a, b) -> b - a }
        assertTrue(d.all { kotlin.math.abs(it - d.first()) < 0.01 })
    }

    @Test
    fun `restore from silence or toward a lower level never blows up`() {
        assertEquals(0.8f, DuckRamp.restoreLevel(20, 20, 0f, 0.8f), 1e-6f)
        assertTrue(DuckRamp.restoreLevel(1, 20, 0f, 0.8f) <= 0.8f)
        assertEquals(0.2f, DuckRamp.restoreLevel(20, 20, 0.5f, 0.2f), 1e-6f)
        assertEquals(1f, DuckRamp.restoreLevel(20, 20, 0.05f, 1.7f), 1e-6f)
    }
}
