package com.audiplex.app.playback

import android.media.AudioManager
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ExternalDuckLevel6707Test {

    private val fresh = ExternalDuckLevel.FRESH_MS

    @Test
    fun noOfferReturnsNull() {
        assertNull(ExternalDuckLevel().forDuck(0))
    }

    @Test
    fun episodeStaysOpenPastFreshWindow() {
        val d = ExternalDuckLevel()
        d.offer(0.4f, 1000)
        assertEquals(0.4f, d.forDuck(1500)!!, 0f)
        assertEquals(0.4f, d.forDuck(60_000)!!, 0f)
        d.endEpisode()
        assertNull(d.forDuck(60_001))
    }

    @Test
    fun staleOfferIsDropped() {
        val d = ExternalDuckLevel()
        d.offer(0.4f, 0)
        assertNull(d.forDuck(fresh + 1))
        assertNull(d.forDuck(fresh + 2))
        assertNull(d.forDuck(1))
    }

    @Test
    fun futureOfferNotUsed() {
        val d = ExternalDuckLevel()
        d.offer(0.4f, 5000)
        assertNull(d.forDuck(1000))
    }

    @Test
    fun clampsAndIgnoresNaN() {
        val hi = ExternalDuckLevel()
        hi.offer(5f, 0)
        assertEquals(ExternalDuckLevel.MAX, hi.forDuck(10)!!, 0f)
        assertEquals(0.6f, ExternalDuckLevel.MAX, 0f)
        val lo = ExternalDuckLevel()
        lo.offer(0f, 0)
        assertEquals(ExternalDuckLevel.MIN, lo.forDuck(10)!!, 0f)
        assertEquals(0.02f, ExternalDuckLevel.MIN, 0f)
        val nan = ExternalDuckLevel()
        nan.offer(Float.NaN, 0)
        assertNull(nan.forDuck(10))
    }

    @Test
    fun newOfferReplacesEpisodeLevel() {
        val d = ExternalDuckLevel()
        d.offer(0.4f, 0)
        assertEquals(0.4f, d.forDuck(10)!!, 0f)
        d.offer(0.2f, 100)
        assertEquals(0.2f, d.forDuck(200)!!, 0f)
    }

    private val canDuck = AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK

    @Test
    fun duckDefaultUnchanged() {
        val r = FocusPolicy.decide(canDuck, FocusPolicy.FocusState(currentVolume = 1f, preDuckVolume = null, isSpeech = false, externalDuckFraction = null))
        assertEquals(FocusPolicy.FocusAction.Duck(FocusPolicy.DUCK_VOLUME), r)
    }

    @Test
    fun duckUsesExternalFractionOfPreDuckVolume() {
        val r = FocusPolicy.decide(canDuck, FocusPolicy.FocusState(currentVolume = 0.5f, preDuckVolume = 0.8f, isSpeech = false, externalDuckFraction = 0.4f))
        assertTrue(r is FocusPolicy.FocusAction.Duck)
        assertEquals(0.32f, (r as FocusPolicy.FocusAction.Duck).volume, 1e-4f)
    }

    @Test
    fun duckUsesCurrentVolumeWhenNoPreDuck() {
        val r = FocusPolicy.decide(canDuck, FocusPolicy.FocusState(currentVolume = 0.5f, preDuckVolume = null, isSpeech = false, externalDuckFraction = 0.2f))
        assertTrue(r is FocusPolicy.FocusAction.Duck)
        assertEquals(0.1f, (r as FocusPolicy.FocusAction.Duck).volume, 1e-4f)
    }

    @Test
    fun speechStillPausesDespiteExternalFraction() {
        val r = FocusPolicy.decide(canDuck, FocusPolicy.FocusState(currentVolume = 0.5f, preDuckVolume = null, isSpeech = true, externalDuckFraction = 0.4f))
        assertEquals(FocusPolicy.FocusAction.Pause, r)
    }
}
