package com.audiplex.app.playback

import kotlin.math.pow

/**
 * #7109: Per-track loudness normalization via attenuate-only gain.
 *
 * The persona voice is the fixed reference (Todd sets device volume by it).
 * Music should be normalized so every song sits at the same loudness, just under
 * the voice. This is achieved by calculating gain based on each track's measured
 * EBU R128 integrated loudness.
 *
 * The gain is attenuate-only (never boosted), so there is no limiter and no
 * quality trade. If a track's loudness is unknown, a fallback level is used.
 */
object LoudnessGain {
    /**
     * Calculate the linear gain multiplier for a track to reach the target loudness.
     *
     * @param trackLufs The track's measured EBU R128 integrated loudness (LUFS), or null if unmeasured.
     * @param targetLufs The reference loudness level to normalize to (e.g., -20.0 LUFS).
     * @param fallbackLufs A fallback loudness level to use if trackLufs is null, or null to use 1.0 if both are null.
     * @return A linear gain multiplier in the range [0.05, 1.0]. Never boosts (always ≤ 1.0).
     *
     * Formula: gain = min(1.0, 10^((target - lufs) / 20))
     * This is the inverse of the dB formula: dB = 20 * log10(gain).
     *
     * Example:
     * - Track at -14 LUFS, target -20 LUFS: gain = 10^((-20 - (-14)) / 20) = 10^(-0.3) ≈ 0.5
     * - Track at -18 LUFS, target -20 LUFS: gain = 10^((-20 - (-18)) / 20) = 10^(-0.1) ≈ 0.79
     * - Track at -25 LUFS, target -20 LUFS: gain would be 10^(0.25) ≈ 1.78, clamped to 1.0 (no boost)
     */
    fun gainFor(
        trackLufs: Double?,
        targetLufs: Double,
        fallbackLufs: Double?
    ): Float {
        // Use track's loudness, or fall back to the median of all measured tracks,
        // or 1.0 if nothing is known.
        val lufs = trackLufs ?: fallbackLufs ?: return 1.0f

        // gain = 10^((target - lufs) / 20)
        val gainLinear = 10.0.pow((targetLufs - lufs) / 20.0).toFloat()

        // Clamp to [0.05, 1.0]: never boost, never silence from a bad measurement.
        return gainLinear.coerceIn(0.05f, 1.0f)
    }
}
