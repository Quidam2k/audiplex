package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Test
import kotlin.math.pow

/**
 * #7109: tests for per-track loudness normalization (LoudnessGain).
 */
class LoudnessGainTest {

    @Test
    fun gainFor_attenuates_loud_track() {
        // Track at -14 LUFS, target -20 LUFS (6 dB louder than target)
        // Should be attenuated by 10^(-6/20) = 10^(-0.3) ≈ 0.5
        val gain = LoudnessGain.gainFor(-14.0, -20.0, null)
        assertEquals(0.5012f, gain, 0.01f)
        // gain < 1.0 (attenuated)
        assert(gain < 1.0f)
    }

    @Test
    fun gainFor_tracks_quieter_than_target_clamped_to_one() {
        // Track at -25 LUFS, target -20 LUFS (5 dB quieter than target)
        // Would naturally boost by 10^(5/20) ≈ 1.78, but attenuate-only clamps to 1.0
        val gain = LoudnessGain.gainFor(-25.0, -20.0, null)
        assertEquals(1.0f, gain)
    }

    @Test
    fun gainFor_exact_match_no_change() {
        // Track at -20 LUFS, target -20 LUFS
        // gain = 10^(0/20) = 1.0 (no change)
        val gain = LoudnessGain.gainFor(-20.0, -20.0, null)
        assertEquals(1.0f, gain, 0.01f)
    }

    @Test
    fun gainFor_null_track_uses_fallback() {
        // No track loudness; fallback is -22 LUFS, target -20 LUFS
        // gain = 10^((-20 - (-22)) / 20) = 10^(0.1) ≈ 1.26, clamped to 1.0
        val gain = LoudnessGain.gainFor(null, -20.0, -22.0)
        assertEquals(1.0f, gain)  // clamped because it would boost
    }

    @Test
    fun gainFor_both_null_returns_one() {
        // No track loudness, no fallback
        val gain = LoudnessGain.gainFor(null, -20.0, null)
        assertEquals(1.0f, gain)
    }

    @Test
    fun gainFor_clamped_floor_zero_point_zero_five() {
        // A track so loud it would reduce volume to near-silence
        // gain = 10^((-20 - 10) / 20) = 10^(-1.5) ≈ 0.0316
        // Clamped to floor of 0.05 (never silence due to bad measurement)
        val gain = LoudnessGain.gainFor(10.0, -20.0, null)
        assertEquals(0.05f, gain)
    }

    @Test
    fun gainFor_moderate_attenuation() {
        // Track at -18 LUFS, target -20 LUFS (2 dB louder)
        // gain = 10^(-2/20) = 10^(-0.1) ≈ 0.794
        val gain = LoudnessGain.gainFor(-18.0, -20.0, null)
        assertEquals(0.7943f, gain, 0.01f)
        assert(gain > 0.75f && gain < 0.85f)
    }

    @Test
    fun gainFor_fallback_preference_when_track_null() {
        // Fallback at -23 LUFS, target -20 LUFS
        // gain = 10^((-20 - (-23)) / 20) = 10^(0.15) ≈ 1.41, clamped to 1.0
        val gain = LoudnessGain.gainFor(null, -20.0, -23.0)
        assertEquals(1.0f, gain)
    }

    @Test
    fun gainFor_track_preferred_over_fallback() {
        // Track at -18 LUFS (fallback would be ignored)
        // gain = 10^((-20 - (-18)) / 20) = 10^(-0.1) ≈ 0.794
        val gain = LoudnessGain.gainFor(-18.0, -20.0, -23.0)
        assertEquals(0.7943f, gain, 0.01f)
    }

    @Test
    fun effectiveMusicVolume_composes_with_gain() {
        // Simulates the composition of slider volume and per-track gain.
        // Music slider at 0.8 (80%), track gain 0.5 (attenuate to half)
        // Effective volume should be 0.8 * 0.5 = 0.4
        val sliderVolume = 0.8f
        val trackGain = LoudnessGain.gainFor(-14.0, -20.0, null)  // ≈ 0.5
        val effective = (sliderVolume * trackGain).coerceIn(0f, 1f)
        assertEquals(0.4f, effective, 0.01f)
    }

    @Test
    fun effectiveMusicVolume_clamped_to_range() {
        // Even if the math produced something out of range (shouldn't), clamping holds.
        val sliderVolume = 1.1f  // invalid, but test the clamp
        val trackGain = 1.0f
        val effective = (sliderVolume * trackGain).coerceIn(0f, 1f)
        assertEquals(1.0f, effective)
    }
}
