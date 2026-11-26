package com.audiplex.app.playback

import kotlin.math.pow

/**
 * Pantheon #7044 (asg 15018/15020, Todd msgs 68905/68920, rulings 53025/53037): voice and
 * music share one device volume. When Todd presses volume while a Pantheon line is playing,
 * that press is a vote on the VOICE level, so Pantheon keeps the new device level and sends
 * VOICE_VOTE with the stream step. Audiplex then moves the slider of what it is playing the
 * opposite way by the same number of decibels, so the music comes back where it was and only
 * the voice changed. Floor 0, cap 1 (the slider's top): a lower from the top cannot push the
 * music past its slider. Pure; the decibel lookup is passed in.
 */
object VoiceVote {
    const val ACTION = "com.audiplex.app.VOICE_VOTE"  // Pantheon TalkDuckPolicy.VOICE_VOTE_ACTION
    const val EXTRA_BEFORE = "before"
    const val EXTRA_AFTER = "after"
    const val EXTRA_MAX = "max"
    const val STEP_DB = 2.5  // when the platform curve gives nothing usable (API < 28, a flat curve)
    const val MAX_STEPS = 10  // one burst of fat-fingered presses can't zero the music

    /** dB the device level moved by, from the platform curve if it is usable, else [STEP_DB] a step. */
    fun stepDb(before: Int, after: Int, dbOf: (Int) -> Double?): Double {
        val steps = (after - before).coerceIn(-MAX_STEPS, MAX_STEPS)
        if (steps == 0) return 0.0
        val a = dbOf(before)
        val b = dbOf(before + steps)
        val curve = if (a != null && b != null && a.isFinite() && b.isFinite()) b - a else Double.NaN
        // a curve that went the wrong way or not at all is not a measurement
        return if (curve.isFinite() && curve != 0.0 && (curve > 0) == (steps > 0)) curve else steps * STEP_DB
    }

    /** The channel slider after a voice vote of [deltaDb]: the opposite move, clamped to 0..1. */
    fun compensate(slider: Float, deltaDb: Double): Float {
        if (!slider.isFinite() || !deltaDb.isFinite() || deltaDb == 0.0) return slider
        return (slider * 10.0.pow(-deltaDb / 20.0)).toFloat().coerceIn(0f, 1f)
    }
}
