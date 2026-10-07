package com.audiplex.app.playback

/**
 * Pure math for the sleep engine's fade (#1728) and its crossfade-into-bed
 * mode (#3367), kept out of PlaybackManager so it is unit-testable.
 */
object SleepFade {

    /** #3714: the sleep button's fade length and bed level, matching dj_sleep_start's defaults. */
    const val DEFAULT_FADE_SECONDS = 120
    const val BED_VOLUME = 0.5f

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

    /** A relative bed url (e.g. /api/stream/12) resolves against the configured server. */
    fun resolveUrl(url: String, baseUrl: String): String =
        if (url.startsWith("http://") || url.startsWith("https://")) url
        else baseUrl.trimEnd('/') + "/" + url.trimStart('/')
}
