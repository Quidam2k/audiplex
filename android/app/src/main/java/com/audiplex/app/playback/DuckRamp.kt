package com.audiplex.app.playback

/**
 * Pantheon #3597: Todd heard the Talk-ON duck as "a right angle" (volume snapped to
 * 5% in one step). Duck and restore now ramp linearly. Pure, for JVM tests.
 */
object DuckRamp {
    const val DUCK_MS = 400L     // #3597 down: fast enough to clear the HFP mic switch
    const val RESTORE_MS = 500L  // #3597 up; Pantheon #2187: 500 ms on an equal-loudness curve (restoreLevel)
    const val STEP_MS = 25L

    fun steps(durationMs: Long): Int = (durationMs / STEP_MS).toInt().coerceAtLeast(1)

    /** Volume at step [i] of [steps] going from [from] to [to] (clamped 0..1). */
    fun level(i: Int, steps: Int, from: Float, to: Float): Float {
        val t = (i.toFloat() / steps.coerceAtLeast(1)).coerceIn(0f, 1f)
        return (from + (to - from) * t).coerceIn(0f, 1f)
    }

    /** Pantheon #2187: floor for the log curve; the player's 0 is "silent", not -inf dB. */
    const val MIN_GAIN = 0.001f

    /**
     * Pantheon #2187: the restore (un-duck) ramp, linear in DECIBELS rather than amplitude.
     * A linear amplitude rise from 5% reaches ~37% of full in its first third, which the ear
     * hears as a jump; a dB-linear rise climbs evenly. Never above [to], lands exactly on it.
     */
    fun restoreLevel(i: Int, steps: Int, from: Float, to: Float): Float {
        val t = (i.toFloat() / steps.coerceAtLeast(1)).coerceIn(0f, 1f)
        val target = to.coerceIn(0f, 1f)
        if (t >= 1f) return target
        val start = from.coerceIn(MIN_GAIN, 1f)
        if (target <= start) return level(i, steps, from, to)  // not a rise: plain linear
        val g = start * Math.pow((target / start).toDouble(), t.toDouble()).toFloat()
        return g.coerceIn(0f, target)
    }
}
