package com.audiplex.app.playback

import org.junit.Assert.*
import org.junit.Test

class SleepFadeTest {

    @Test
    fun mainFadesFromStartToZero() {
        assertEquals(0.8f, SleepFade.levels(0, 8, 0.8f, 0f, null).first, 1e-6f)
        assertEquals(0.4f, SleepFade.levels(4, 8, 0.8f, 0f, null).first, 1e-6f)
        assertEquals(0f, SleepFade.levels(8, 8, 0.8f, 0f, null).first, 1e-6f)
    }

    @Test
    fun bedUntouchedWithoutTarget() {
        // "under" mode / legacy payloads: the bed keeps its own volume.
        assertNull(SleepFade.levels(4, 8, 1f, 0.5f, null).second)
    }

    @Test
    fun bedCrossfadesUpAsMainGoesDown() {
        val steps = 10
        var lastBed = -1f
        for (i in 0..steps) {
            val (main, bed) = SleepFade.levels(i, steps, 1f, 0f, 0.5f)
            assertNotNull(bed)
            assertTrue("bed must rise monotonically", bed!! >= lastBed)
            lastBed = bed
            assertEquals(1f - i / steps.toFloat(), main, 1e-6f)
        }
        assertEquals(0.5f, lastBed, 1e-6f)
    }

    @Test
    fun bedTargetIsClamped() {
        assertEquals(1f, SleepFade.levels(1, 1, 1f, 0f, 3f).second!!, 1e-6f)
    }

    @Test
    fun zeroStepsDoesNotDivideByZero() {
        val (main, bed) = SleepFade.levels(0, 0, 1f, 0f, 0.5f)
        assertEquals(1f, main, 1e-6f)
        assertEquals(0f, bed!!, 1e-6f)
    }

    @Test
    fun resolveUrlKeepsAbsoluteAndJoinsRelative() {
        assertEquals("http://x:1/a.mp3", SleepFade.resolveUrl("http://x:1/a.mp3", "http://solace:8100/"))
        assertEquals("http://solace:8100/api/stream/7", SleepFade.resolveUrl("/api/stream/7", "http://solace:8100/"))
        assertEquals("http://solace:8100/api/stream/7", SleepFade.resolveUrl("api/stream/7", "http://solace:8100"))
    }

    @Test
    fun minutesLeftRoundsUpAndNeverGoesNegative() {  // #3714
        assertEquals(30, SleepFade.minutesLeft(30 * 60_000L, 0))
        assertEquals(30, SleepFade.minutesLeft(30 * 60_000L - 1, 0))
        assertEquals(1, SleepFade.minutesLeft(1_000, 0))
        assertEquals(0, SleepFade.minutesLeft(0, 0))
        assertEquals(0, SleepFade.minutesLeft(0, 5_000))
    }
}
