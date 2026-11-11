package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SkipFadeTest {
    @Test fun outGoesDownToExactlyZero() {
        val g = SkipFade.outGains()
        assertEquals(SkipFade.STEPS_OUT, g.size)
        assertEquals(0f, g.last(), 0f)
        assertTrue(g.zipWithNext().all { (a, b) -> b <= a })
        assertTrue(g.first() < 1f)
    }

    @Test fun inGoesUpToExactlyOne() {
        val g = SkipFade.inGains()
        assertEquals(SkipFade.STEPS_IN, g.size)
        assertEquals(1f, g.last(), 0f)
        assertTrue(g.zipWithNext().all { (a, b) -> b >= a })
        assertTrue(g.first() > 0f)
    }

    @Test fun equalPowerSumsToOneAtEveryPoint() {
        // out(t)^2 + in(t)^2 == 1 where both curves share a time point
        val n = 8
        val o = SkipFade.outGains(n).dropLast(1)
        val i = SkipFade.inGains(n).dropLast(1)
        o.zip(i).forEach { (a, b) -> assertEquals(1f, a * a + b * b, 1e-5f) }
    }

    @Test fun oneStepIsJustTheEndpoint() {
        assertEquals(listOf(0f), SkipFade.outGains(1))
        assertEquals(listOf(1f), SkipFade.inGains(1))
    }

    @Test fun onlyFadesInPublicModeMidSong() {
        assertTrue(SkipFade.shouldFade(true, true, 10_000, 200_000, false))
        assertFalse(SkipFade.shouldFade(false, true, 10_000, 200_000, false))   // private: hard cut
        assertFalse(SkipFade.shouldFade(true, false, 10_000, 200_000, false))   // paused
        assertFalse(SkipFade.shouldFade(true, true, 10_000, 200_000, true))     // ducked
        assertFalse(SkipFade.shouldFade(true, true, 199_000, 200_000, false))   // at the boundary
        assertFalse(SkipFade.shouldFade(true, true, 10_000, -1, false))         // duration unknown
    }
}
