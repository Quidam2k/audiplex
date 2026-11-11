package com.audiplex.app.playback

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/**
 * #5697: in public DJ mode a skip fades the skipped song out and the next one in instead of
 * cutting hard. Pure math for JVM tests; PlaybackManager drives the steps.
 */
object SkipFade {
    const val OUT_MS = 300L
    const val IN_MS = 400L
    const val STEPS_OUT = 12
    const val STEPS_IN = 16

    /** Todd: a skip right at the boundary between songs doesn't matter; no point fading there. */
    const val MIN_REMAINING_MS = 1_500L

    /** Public DJ mode, music playing, not already ducked, and not about to end anyway. */
    fun shouldFade(
        publicDj: Boolean, playing: Boolean, positionMs: Long, durationMs: Long, ducked: Boolean,
    ): Boolean =
        publicDj && playing && !ducked && durationMs > 0 && durationMs - positionMs > MIN_REMAINING_MS

    /** Equal-power gains for the fade-out: [steps] values after full, ending exactly at 0. */
    fun outGains(steps: Int = STEPS_OUT): List<Float> {
        val n = steps.coerceAtLeast(1)
        return (1..n).map { i -> if (i == n) 0f else cos(i.toDouble() / n * PI / 2).toFloat() }
    }

    /** Equal-power gains for the fade-in: [steps] values from near silence, ending exactly at 1. */
    fun inGains(steps: Int = STEPS_IN): List<Float> {
        val n = steps.coerceAtLeast(1)
        return (1..n).map { i -> if (i == n) 1f else sin(i.toDouble() / n * PI / 2).toFloat() }
    }
}
