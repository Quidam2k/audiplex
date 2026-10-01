package com.audiplex.app.playback

/**
 * Pantheon #3597: Todd heard the Talk-ON duck as "a right angle" (volume snapped to
 * 5% in one step). Duck and restore now ramp linearly. Pure, for JVM tests.
 */
object DuckRamp {
    const val DUCK_MS = 400L     // #3597 down: fast enough to clear the HFP mic switch
    const val RESTORE_MS = 600L  // #3597 up: a little slower, rises under the voice's tail
    const val STEP_MS = 25L

    fun steps(durationMs: Long): Int = (durationMs / STEP_MS).toInt().coerceAtLeast(1)

    /** Volume at step [i] of [steps] going from [from] to [to] (clamped 0..1). */
    fun level(i: Int, steps: Int, from: Float, to: Float): Float {
        val t = (i.toFloat() / steps.coerceAtLeast(1)).coerceIn(0f, 1f)
        return (from + (to - from) * t).coerceIn(0f, 1f)
    }
}
