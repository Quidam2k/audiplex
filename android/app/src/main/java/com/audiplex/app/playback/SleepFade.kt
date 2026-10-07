package com.audiplex.app.playback

import kotlinx.coroutines.delay

/**
 * Pure math for the sleep engine's fade (#1728) and its crossfade-into-bed
 * mode (#3367), kept out of PlaybackManager so it is unit-testable.
 */
object SleepFade {

    /** #3714: the sleep button's fade length and bed level, matching dj_sleep_start's defaults. */
    const val DEFAULT_FADE_SECONDS = 120
    const val BED_VOLUME = 0.5f

    /** #3953: the dialog's "Test: 1 min" chip uses a short fade so the whole handoff is heard in ~75 s. */
    const val TEST_FADE_SECONDS = 15

    /** Whole minutes until [endsAtMs], rounded up so "1 min" shows until it starts; never negative. */
    fun minutesLeft(endsAtMs: Long, nowMs: Long): Int =
        ((endsAtMs - nowMs).coerceAtLeast(0) + 59_999).div(60_000).toInt()

    /**
     * Volumes at step [i] of [steps]: the main player ramps linearly from
     * [mainStart] to 0; when [bedTarget] is set the bed ramps linearly from
     * [bedStart] to it over the same window. Bed is null when it should be
     * left alone.
     */
    fun levels(i: Int, steps: Int, mainStart: Float, bedStart: Float, bedTarget: Float?): Pair<Float, Float?> {
        val t = (i.toFloat() / steps.coerceAtLeast(1)).coerceIn(0f, 1f)
        val main = mainStart * (1f - t)
        val bed = bedTarget?.let { target ->
            (bedStart + (target.coerceIn(0f, 1f) - bedStart) * t).coerceIn(0f, 1f)
        }
        return main to bed
    }

    /**
     * The sleep timer itself (#3953), pulled out of PlaybackManager.startSleepTimer
     * unchanged so a test can run it on virtual time: wait [minutes], then fade
     * the main player out over [fadeSeconds] (ramping the bed to [bedFadeTo] when
     * set), then pause. The bed is never stopped here; it plays on until bedStop.
     * Returns false, without pausing, when [mainVolume] says no player is there.
     */
    suspend fun runTimer(
        minutes: Float,
        fadeSeconds: Int,
        bedFadeTo: Float?,
        mainVolume: () -> Float?,
        bedVolume: () -> Float,
        setMain: (Float) -> Unit,
        setBed: (Float) -> Unit,
        pause: () -> Unit,
    ): Boolean {
        delay((minutes * 60_000).toLong().coerceAtLeast(0))
        val startVolume = mainVolume() ?: return false
        val bedStart = bedVolume()
        val steps = (fadeSeconds.coerceAtLeast(1) * 4).coerceAtLeast(1)
        val stepDelayMs = (fadeSeconds * 1000L / steps).coerceAtLeast(50L)
        for (i in 0..steps) {
            val (main, bed) = levels(i, steps, startVolume, bedStart, bedFadeTo)
            if (startVolume > 0f) setMain(main)
            if (bed != null) setBed(bed)
            delay(stepDelayMs)
        }
        pause()
        return true
    }

    /** #3953: the bed the dialog preselects: the last one used if it is still offered, else the first. Never Silence when beds load. */
    fun defaultBedId(bedIds: List<Int>, lastUsed: Int?): Int? =
        lastUsed?.takeIf { it in bedIds } ?: bedIds.firstOrNull()

    /** A relative bed url (e.g. /api/stream/12) resolves against the configured server. */
    fun resolveUrl(url: String, baseUrl: String): String =
        if (url.startsWith("http://") || url.startsWith("https://")) url
        else baseUrl.trimEnd('/') + "/" + url.trimStart('/')
}
