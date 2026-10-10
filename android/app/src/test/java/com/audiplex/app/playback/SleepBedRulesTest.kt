package com.audiplex.app.playback

import org.junit.Assert.*
import org.junit.Test

class SleepBedRulesTest {

    @Test
    fun `first three failures retry after ten thirty and sixty seconds`() {
        assertEquals(SleepBedRules.Recovery.Retry(10_000L), SleepBedRules.recoveryFor(1, false, true))
        assertEquals(SleepBedRules.Recovery.Retry(30_000L), SleepBedRules.recoveryFor(2, false, true))
        assertEquals(SleepBedRules.Recovery.Retry(60_000L), SleepBedRules.recoveryFor(3, false, true))
    }

    @Test
    fun `fourth failure switches to an available fallback`() {
        assertEquals(SleepBedRules.Recovery.Fallback, SleepBedRules.recoveryFor(4, false, true))
    }

    @Test
    fun `fourth failure gives up when already on fallback or none is available`() {
        assertEquals(SleepBedRules.Recovery.GiveUp, SleepBedRules.recoveryFor(4, true, true))
        assertEquals(SleepBedRules.Recovery.GiveUp, SleepBedRules.recoveryFor(4, false, false))
    }

    @Test
    fun `zero failures is treated as the first failure`() {
        assertEquals(SleepBedRules.Recovery.Retry(10_000L), SleepBedRules.recoveryFor(0, false, true))
    }

    @Test
    fun `unwanted bed is off even when playing or recovering`() {
        assertEquals(BedState.Off, SleepBedRules.stateFor(false, false, true, 3))
        assertEquals(BedState.Off, SleepBedRules.stateFor(false, true, true, 3))
    }

    @Test
    fun `wanted bed is recovering while recovery is scheduled`() {
        assertEquals(BedState.Recovering, SleepBedRules.stateFor(true, true, true, 3))
        assertEquals(BedState.Recovering, SleepBedRules.stateFor(true, true, false, 1))
    }

    @Test
    fun `buffering or ready bed is playing when playWhenReady is set`() {
        assertEquals(BedState.Playing, SleepBedRules.stateFor(true, false, true, 2))
        assertEquals(BedState.Playing, SleepBedRules.stateFor(true, false, true, 3))
    }

    @Test
    fun `paused ready idle and ended beds are stopped`() {
        assertEquals(BedState.Stopped, SleepBedRules.stateFor(true, false, false, 3))
        assertEquals(BedState.Stopped, SleepBedRules.stateFor(true, false, true, 1))
        assertEquals(BedState.Stopped, SleepBedRules.stateFor(true, false, true, 4))
    }

    @Test
    fun `paused book requires foreground while bed plays or recovers`() {
        assertTrue(SleepBedRules.foregroundRequired(false, BedState.Playing))
        assertTrue(SleepBedRules.foregroundRequired(false, BedState.Recovering))
        assertFalse(SleepBedRules.foregroundRequired(false, BedState.Stopped))
        assertFalse(SleepBedRules.foregroundRequired(false, BedState.Off))
    }

    @Test
    fun `required media keeps foreground regardless of bed state`() {
        for (bed in BedState.values()) {
            assertTrue(SleepBedRules.foregroundRequired(true, bed))
        }
    }

    @Test
    fun `LAN tailnet queried and relative URLs share the same cache file`() {
        val lan = SleepBedRules.cacheFileName("http://192.168.1.10:8100/api/stream/610")
        val tailnet = SleepBedRules.cacheFileName("http://100.64.0.10:8100/api/stream/610")
        assertEquals("bed_api_stream_610", lan)
        assertEquals(lan, tailnet)
        assertEquals(lan, SleepBedRules.cacheFileName("http://host/api/stream/610?token=abc"))
        assertEquals(lan, SleepBedRules.cacheFileName("/api/stream/610"))
        assertEquals(lan, SleepBedRules.cacheFileName("api/stream/610"))
        assertEquals("bed_root", SleepBedRules.cacheFileName("http://host"))
    }

    @Test
    fun `brown title wins regardless of position or case`() {
        val brown = "BrOwN noise" to "/brown"
        val rain = "Rain" to "/rain"
        val waves = "Waves" to "/waves"
        assertEquals("/brown", SleepBedRules.pickFallback(listOf(brown, rain, waves)))
        assertEquals("/brown", SleepBedRules.pickFallback(listOf(rain, brown, waves)))
        assertEquals("/brown", SleepBedRules.pickFallback(listOf(rain, waves, brown)))
    }

    @Test
    fun `fallback is the last bed without a brown title and null for an empty list`() {
        assertEquals("/waves", SleepBedRules.pickFallback(listOf("Rain" to "/rain", "Waves" to "/waves")))
        assertNull(SleepBedRules.pickFallback(emptyList()))
    }
}
