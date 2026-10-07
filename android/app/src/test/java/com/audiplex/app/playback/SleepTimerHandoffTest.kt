package com.audiplex.app.playback

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.currentTime
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.*
import org.junit.Test

/** #3953 (10/6): the timer ended in silence instead of the sleep bed. */
@OptIn(ExperimentalCoroutinesApi::class)
class SleepTimerHandoffTest {
    private class Fake(var main: Float = 1f, var bed: Float = 0f) {
        var paused = false
        var setMainCalls = 0
        var setBedCalls = 0
        var pauseCalls = 0

        suspend fun run(
            minutes: Float = 1f,
            fadeSeconds: Int = 15,
            bedFadeTo: Float? = SleepFade.BED_VOLUME,
            playerPresent: Boolean = true,
        ): Boolean = SleepFade.runTimer(
            minutes = minutes,
            fadeSeconds = fadeSeconds,
            bedFadeTo = bedFadeTo,
            mainVolume = { if (playerPresent) main else null },
            bedVolume = { bed },
            setMain = { main = it; setMainCalls++ },
            setBed = { bed = it; setBedCalls++ },
            pause = { paused = true; pauseCalls++ },
        )
    }

    @Test
    fun timerEndStartsTheBed() = runTest {
        val fake = Fake(main = 1f, bed = 0f)
        var result: Boolean? = null
        val job = launch { result = fake.run(minutes = 1f, fadeSeconds = 15) }
        advanceTimeBy(59_999L)
        runCurrent()
        assertEquals(0f, fake.bed, 1e-4f)
        assertEquals(1f, fake.main, 1e-4f)
        assertFalse(fake.paused)
        advanceTimeBy(15_000L + 1_000L)
        runCurrent()
        assertTrue(job.isCompleted)
        assertEquals(true, result)
        assertTrue(fake.paused)
        assertEquals(1, fake.pauseCalls)
        assertEquals(0f, fake.main, 1e-4f)
        assertEquals(SleepFade.BED_VOLUME, fake.bed, 1e-4f)
    }

    @Test
    fun bedRampsDuringFade() = runTest {
        val fake = Fake()
        launch { fake.run() }
        advanceTimeBy(60_000L + 7_500L)
        runCurrent()
        assertTrue(fake.bed > 0f && fake.bed < SleepFade.BED_VOLUME)
        assertTrue(fake.main > 0f && fake.main < 1f)
        assertFalse(fake.paused)
    }

    @Test
    fun silencePathLeavesBedAlone() = runTest {
        val fake = Fake(bed = 0f)
        val job = launch { fake.run(bedFadeTo = null) }
        advanceTimeBy(76_000L)
        runCurrent()
        assertTrue(job.isCompleted)
        assertEquals(0, fake.setBedCalls)
        assertEquals(0f, fake.bed, 1e-4f)
        assertTrue(fake.paused)
        assertEquals(0f, fake.main, 1e-4f)
    }

    @Test
    fun noPlayerMeansNoPause() = runTest {
        val fake = Fake()
        assertFalse(fake.run(playerPresent = false))
        assertFalse(fake.paused)
        assertEquals(0, fake.pauseCalls)
        assertEquals(0, fake.setMainCalls)
    }

    @Test
    fun existingDefaultPathUnchanged() = runTest {
        val fake = Fake()
        var completedAt = -1L
        val job = launch {
            fake.run(minutes = 30f, fadeSeconds = SleepFade.DEFAULT_FADE_SECONDS)
            completedAt = currentTime
        }
        advanceTimeBy(30 * 60_000L + 119_000L)
        runCurrent()
        assertFalse(job.isCompleted)
        advanceTimeBy(2_000L)
        runCurrent()
        assertTrue(job.isCompleted)
        assertEquals(30 * 60_000L + 120_000L + 250L, completedAt)
        assertEquals(481, fake.setMainCalls)
    }

    @Test
    fun defaultBedPrefersLastUsedThenFirstNeverSilence() {
        assertEquals(609, SleepFade.defaultBedId(listOf(610, 609), 609))
        assertEquals(610, SleepFade.defaultBedId(listOf(610, 609), 999))
        assertEquals(610, SleepFade.defaultBedId(listOf(610, 609), null))
        assertNull(SleepFade.defaultBedId(emptyList(), 609))
    }
}
