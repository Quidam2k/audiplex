// #5282
package com.audiplex.app.playback

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.*
import org.junit.Test
import kotlin.math.log10
import kotlin.math.pow

/** #5282: sleep-bed sliders, the one-time migration, the book's net gain, the morning hold, and the ramps. */
@OptIn(ExperimentalCoroutinesApi::class)
class SleepBedMixTest {

    private fun dbToAmp(db: Double): Float = 10.0.pow(db / 20.0).toFloat()

    // ---- 1. sliders ----

    @Test
    fun sliderTopIsUnityAndNothingGoesAbove() {
        assertEquals(1f, SleepBedRules.sliderToLevel(1f), 0f)
        for (p in listOf(1.0001f, 1.5f, 100f, Float.POSITIVE_INFINITY)) {
            assertTrue("p=$p", SleepBedRules.sliderToLevel(p) <= 1f)
        }
        assertEquals(0f, SleepBedRules.sliderToLevel(Float.NaN), 0f)
    }

    @Test
    fun sliderZeroIsExactlySilentAndHalfIsMinus20Db() {
        assertEquals(0f, SleepBedRules.sliderToLevel(0f), 0f)
        assertEquals(0f, SleepBedRules.sliderToLevel(-0.5f), 0f)
        assertEquals(0.1f, SleepBedRules.sliderToLevel(0.5f), 1e-5f)
    }

    @Test
    fun levelToSliderInvertsSliderToLevel() {
        var p = 0.01f
        while (p <= 1f) {
            assertEquals("p=$p", p, SleepBedRules.levelToSlider(SleepBedRules.sliderToLevel(p)), 1e-3f)
            p += 0.01f
        }
        // the other direction; 0.01 itself is -40 dB, the slider's silent end, so start just above it
        var l = 0.02f
        while (l <= 1f) {
            assertEquals("l=$l", l, SleepBedRules.sliderToLevel(SleepBedRules.levelToSlider(l)), 1e-3f)
            l += 0.01f
        }
    }

    @Test
    fun sliderIsMonotonic() {
        var prevLevel = SleepBedRules.sliderToLevel(0.01f)
        var p = 0.02f
        while (p <= 1f) {
            val level = SleepBedRules.sliderToLevel(p)
            assertTrue("p=$p", level > prevLevel)
            prevLevel = level
            p += 0.01f
        }
        var prevSlider = SleepBedRules.levelToSlider(0.01f)
        var l = 0.02f
        while (l <= 1f) {
            val s = SleepBedRules.levelToSlider(l)
            assertTrue("l=$l", s >= prevSlider)
            prevSlider = s
            l += 0.01f
        }
    }

    // ---- 2. DJ levels ----

    @Test
    fun fromDjLevelIsIdentityClampedAndNanIsZero() {
        for (x in listOf(0f, 0.1f, 0.5f, 0.9f, 1f)) assertEquals(x, SleepBedRules.fromDjLevel(x), 0f)
        assertEquals(0f, SleepBedRules.fromDjLevel(-0.3f), 0f)
        assertEquals(1f, SleepBedRules.fromDjLevel(2f), 0f)
        assertEquals(0f, SleepBedRules.fromDjLevel(Float.NaN), 0f)
        assertEquals(0.9f, SleepBedRules.DEFAULT_VOLUME, 0f)
    }

    // ---- 3. migration off the #5131 boost scale ----

    @Test
    fun migrationKeepsBalanceForBoostLevels() {
        val m = SleepBedRules.migrateToAttenuate(0.87f, headroomMigrated = true, alreadyMigrated = false)!!
        assertEquals(1f, m.bedLevel, 0f)
        assertEquals(dbToAmp(-8.88), m.bookLevel, 1e-3f)

        val max = SleepBedRules.migrateToAttenuate(1f, true, false)!!
        assertEquals(1f, max.bedLevel, 0f)
        assertEquals(dbToAmp(-12.0), max.bookLevel, 1e-3f)
    }

    @Test
    fun migrationBedOverBookDifferenceEqualsTheOldBoost() {
        for (stored in listOf(0.55f, 0.6f, 0.7f, 0.87f, 0.95f, 1f)) {
            val m = SleepBedRules.migrateToAttenuate(stored, true, false)!!
            val oldBoostDb = 12.0 * (stored - 0.5) / 0.5
            val diffDb = 20.0 * log10(m.bedLevel.toDouble() / m.bookLevel.toDouble())
            assertEquals("stored=$stored", oldBoostDb, diffDb, 0.01)
        }
    }

    @Test
    fun migrationBelowUnityOnlyScalesTheBed() {
        val unity = SleepBedRules.migrateToAttenuate(0.5f, true, false)!!
        assertEquals(1f, unity.bedLevel, 0f)
        assertEquals(1f, unity.bookLevel, 0f)
        val quarter = SleepBedRules.migrateToAttenuate(0.25f, true, false)!!
        assertEquals(0.5f, quarter.bedLevel, 1e-6f)
        assertEquals(1f, quarter.bookLevel, 0f)
    }

    @Test
    fun migrationDoesNothingWhenNotNeeded() {
        assertNull(SleepBedRules.migrateToAttenuate(null, true, false))
        assertNull(SleepBedRules.migrateToAttenuate(0.87f, true, alreadyMigrated = true))
        assertNull(SleepBedRules.migrateToAttenuate(0.87f, headroomMigrated = false, alreadyMigrated = false))
    }

    @Test
    fun migratedBedIsNeverAboveOne() {
        var s = 0f
        while (s <= 1.5f) {
            val m = SleepBedRules.migrateToAttenuate(s, true, false)!!
            assertTrue("s=$s", m.bedLevel <= 1f && m.bedLevel >= 0f)
            assertTrue("s=$s", m.bookLevel <= 1f && m.bookLevel >= 0f)
            s += 0.05f
        }
    }

    // ---- 4. book net gain ----

    @Test
    fun bookNetDbAddsTheTrimToTheGain() {
        assertEquals(-5.85, SleepBedRules.bookNetDb(3.05, dbToAmp(-8.9)), 0.01)
    }

    @Test
    fun bookNetDbNeverGoesPastZero() {
        assertEquals(0.0, SleepBedRules.bookNetDb(3.05, 1f), 1e-9)
        assertEquals(-2.0, SleepBedRules.bookNetDb(-2.0, 1f), 1e-9)
    }

    @Test
    fun bookNetDbAtLevelZeroIsSilence() {
        val db = SleepBedRules.bookNetDb(3.05, 0f)
        assertEquals(Double.NEGATIVE_INFINITY, db, 0.0)
        assertEquals(0.0, 10.0.pow(db / 20.0), 0.0)
    }

    @Test
    fun bookNetDbIsNeverPositive() {
        var gain = -10.0
        while (gain <= 6.0) {
            for (level in listOf(0f, 0.01f, 0.1f, 0.3f, 0.5f, 0.9f, 1f)) {
                assertTrue("gain=$gain level=$level", SleepBedRules.bookNetDb(gain, level) <= 0.0)
            }
            gain += 0.5
        }
    }

    // ---- 5. morning hold ----

    private val jbl = "jbl"
    private val headset = "headset"

    @Test
    fun holdActiveOnlyOnSameRouteAboveTheStart() {
        val hold = SleepBedRules.SleepHold(7, jbl)
        assertFalse(SleepBedRules.holdActive(null, 11, jbl))
        assertTrue(SleepBedRules.holdActive(hold, 8, jbl))
        assertFalse(SleepBedRules.holdActive(hold, 7, jbl))
        assertFalse(SleepBedRules.holdActive(hold, 3, jbl))
        assertFalse(SleepBedRules.holdActive(hold, 15, headset))
    }

    @Test
    fun holdClearsOnlyOnSameRouteAtOrBelowTheStart() {
        val hold = SleepBedRules.SleepHold(7, jbl)
        assertTrue(SleepBedRules.holdCleared(hold, 7, jbl))
        assertTrue(SleepBedRules.holdCleared(hold, 4, jbl))
        assertFalse(SleepBedRules.holdCleared(hold, 8, jbl))
        assertFalse(SleepBedRules.holdCleared(hold, 7, headset))
        assertFalse(SleepBedRules.holdCleared(hold, 2, headset))
    }

    @Test
    fun nextHoldKeepsTheLowerStartOnTheSameRoute() {
        assertEquals(SleepBedRules.SleepHold(9, jbl), SleepBedRules.nextHold(null, 9, jbl))
        val first = SleepBedRules.SleepHold(7, jbl)
        // a second bedtime started at the raised 11 keeps the first night's 7
        assertEquals(SleepBedRules.SleepHold(7, jbl), SleepBedRules.nextHold(first, 11, jbl))
        // and the other way round: a lower start wins
        assertEquals(SleepBedRules.SleepHold(5, jbl), SleepBedRules.nextHold(first, 5, jbl))
        // another output replaces it
        assertEquals(SleepBedRules.SleepHold(15, headset), SleepBedRules.nextHold(first, 15, headset))
    }

    @Test
    fun morningScenario() {
        // Bedtime at volume index 7 on the JBL.
        val hold = SleepBedRules.nextHold(null, 7, jbl)
        // Phone raised to 11 overnight: Audiplex's cut stays on, the hold is not cleared.
        assertTrue(SleepBedRules.holdActive(hold, 11, jbl))
        assertFalse(SleepBedRules.holdCleared(hold, 11, jbl))
        // Headset at index 15: not active, and it does not clear the JBL hold.
        assertFalse(SleepBedRules.holdActive(hold, 15, headset))
        assertFalse(SleepBedRules.holdCleared(hold, 15, headset))
        // Back on the JBL still raised: still held.
        assertTrue(SleepBedRules.holdActive(hold, 11, jbl))
        // Phone back down to 7: the hold is cleared and no longer active.
        assertTrue(SleepBedRules.holdCleared(hold, 7, jbl))
        assertFalse(SleepBedRules.holdActive(hold, 7, jbl))
    }

    // ---- 7. ramp ----

    @Test
    fun rampDefaultsToSixteenStepsEndingExactlyAtTarget() {
        val r = SleepBedRules.rampVolumes(1f, 0.3f)
        assertEquals(16, r.size)
        assertEquals(SleepBedRules.BOOK_RAMP_STEPS, r.size)
        assertEquals(0.3f, r.last(), 0f)
        assertEquals(5, SleepBedRules.rampVolumes(0f, 1f, 5).size)
        assertEquals(1f, SleepBedRules.rampVolumes(0f, 1f, 5).last(), 0f)
    }

    @Test
    fun rampIsClampedAndMonotonic() {
        val down = SleepBedRules.rampVolumes(1f, 0.2f)
        assertTrue(down.zipWithNext().all { (a, b) -> b <= a })
        val up = SleepBedRules.rampVolumes(0.2f, 1f)
        assertTrue(up.zipWithNext().all { (a, b) -> b >= a })
        val wild = SleepBedRules.rampVolumes(-0.5f, 1.7f, 8)
        assertTrue(wild.all { it in 0f..1f })
        assertEquals(1f, wild.last(), 0f)
        val wildDown = SleepBedRules.rampVolumes(1.7f, -0.5f, 8)
        assertTrue(wildDown.all { it in 0f..1f })
        assertEquals(0f, wildDown.last(), 0f)
    }

    // ---- 8. SleepFade.runTimer fade-start hook ----

    @Test
    fun onFadeStartFiresOnceAfterTheWaitAndBeforeAnyVolumeChange() = runTest {
        val events = mutableListOf<String>()
        var fadeStarts = 0
        val job = launch {
            SleepFade.runTimer(
                minutes = 1f,
                fadeSeconds = 15,
                bedFadeTo = SleepFade.BED_VOLUME,
                mainVolume = { 1f },
                bedVolume = { 0f },
                setMain = { events += "main" },
                setBed = { events += "bed" },
                pause = { events += "pause" },
                onFadeStart = { fadeStarts++; events += "start" },
            )
        }
        advanceTimeBy(59_999L)
        runCurrent()
        assertEquals(0, fadeStarts)
        assertTrue(events.isEmpty())

        advanceTimeBy(1L)
        runCurrent()
        assertEquals(1, fadeStarts)
        assertEquals("start", events.first())

        advanceTimeBy(16_000L)
        runCurrent()
        assertTrue(job.isCompleted)
        assertEquals(1, fadeStarts)
        assertEquals(1, events.count { it == "start" })
        assertTrue(events.indexOf("start") < events.indexOf("main"))
        assertTrue(events.indexOf("start") < events.indexOf("bed"))
        assertEquals("pause", events.last())
    }

    @Test
    fun onFadeStartNotCalledWhenThereIsNoPlayer() = runTest {
        var fadeStarts = 0
        val result = SleepFade.runTimer(
            minutes = 1f,
            fadeSeconds = 15,
            bedFadeTo = SleepFade.BED_VOLUME,
            mainVolume = { null },
            bedVolume = { 0f },
            setMain = {},
            setBed = {},
            pause = {},
            onFadeStart = { fadeStarts++ },
        )
        assertFalse(result)
        assertEquals(0, fadeStarts)
    }

    @Test
    fun holdEndsAfterItsTimeSoARaisedHeadsetIsNotQuietOnTomorrowsRide() {
        val hold = SleepBedRules.SleepHold(7, jbl, atMs = 1_000L, finished = true)
        val lastMoment = 1_000L + SleepBedRules.HOLD_MAX_MS - 1
        assertTrue(SleepBedRules.holdActive(hold, 11, jbl, lastMoment))
        assertFalse(SleepBedRules.holdCleared(hold, 11, jbl, lastMoment))
        assertFalse(SleepBedRules.holdActive(hold, 11, jbl, lastMoment + 1))
        assertTrue(SleepBedRules.holdCleared(hold, 11, jbl, lastMoment + 1))
    }

    @Test
    fun noRaiseAtBedtimeLeavesNoHold() {
        // A preview or a cancelled timer at 3, then a deliberate 8 later, must not be cut.
        val open = SleepBedRules.nextHold(null, 3, jbl, 100L)
        assertEquals(null, SleepBedRules.finishHold(open, 3, jbl, 200L))
    }

    @Test
    fun raiseAtBedtimeHoldsAndStartsTheClockAtTheEnd() {
        val open = SleepBedRules.nextHold(null, 7, jbl, 100L)
        val held = SleepBedRules.finishHold(open, 11, jbl, 5_000L)!!
        assertEquals(SleepBedRules.SleepHold(7, jbl, 5_000L, true), held)
        // A later bed_stop (the chimes ride the bed player) does not restart the clock.
        assertEquals(held, SleepBedRules.finishHold(held, 11, jbl, 9_000L))
    }

    @Test
    fun anExpiredHoldDoesNotLendItsLowerStartToANewBedtime() {
        val old = SleepBedRules.SleepHold(3, jbl, atMs = 0L, finished = true)
        assertEquals(
            SleepBedRules.SleepHold(9, jbl, SleepBedRules.HOLD_MAX_MS),
            SleepBedRules.nextHold(old, 9, jbl, SleepBedRules.HOLD_MAX_MS),
        )
    }
}
