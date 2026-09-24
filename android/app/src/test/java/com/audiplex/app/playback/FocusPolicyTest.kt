package com.audiplex.app.playback

import android.media.AudioManager
import org.junit.Assert.*
import org.junit.Test

class FocusPolicyTest {

    @Test
    fun testMusicDucksOnTransientCanDuck() {
        val state = FocusPolicy.FocusState(currentVolume = 0.6f, isSpeech = false)
        val action = FocusPolicy.decide(AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK, state)
        assertEquals(FocusPolicy.FocusAction.Duck(0.05f), action)
    }

    @Test
    fun testSpeechPausesOnTransientCanDuck() {
        // #3099/#991 narrator rule: audiobooks pause under TTS instead of ducking.
        val state = FocusPolicy.FocusState(currentVolume = 1f, isSpeech = true)
        val action = FocusPolicy.decide(AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK, state)
        assertEquals(FocusPolicy.FocusAction.Pause, action)
    }

    @Test
    fun testPauseOnTransientNoDuck() {
        val state = FocusPolicy.FocusState(currentVolume = 1f)
        val action = FocusPolicy.decide(AudioManager.AUDIOFOCUS_LOSS_TRANSIENT, state)
        assertEquals(FocusPolicy.FocusAction.Pause, action)
    }

    @Test
    fun testPauseAndAbandonOnFullLoss() {
        val state = FocusPolicy.FocusState(currentVolume = 1f)
        val action = FocusPolicy.decide(AudioManager.AUDIOFOCUS_LOSS, state)
        assertEquals(FocusPolicy.FocusAction.PauseAndAbandon, action)
    }

    @Test
    fun testGainRestoresPreDuckVolumeNotFullScale() {
        // Dial was at 0.6 before the duck; GAIN must go back to 0.6, never 1.0.
        val state = FocusPolicy.FocusState(currentVolume = 0.05f, preDuckVolume = 0.6f)
        val action = FocusPolicy.decide(AudioManager.AUDIOFOCUS_GAIN, state)
        assertEquals(FocusPolicy.FocusAction.Restore(volume = 0.6f, resume = false), action)
    }

    @Test
    fun testGainResumesWhenPolicyPausedIt() {
        val state = FocusPolicy.FocusState(wasPlaying = true, currentVolume = 0.8f)
        val action = FocusPolicy.decide(AudioManager.AUDIOFOCUS_GAIN, state)
        assertEquals(FocusPolicy.FocusAction.Restore(volume = 0.8f, resume = true), action)
    }

    @Test
    fun testGainWithoutDuckKeepsCurrentVolume() {
        val state = FocusPolicy.FocusState(currentVolume = 0.35f, preDuckVolume = null)
        val action = FocusPolicy.decide(AudioManager.AUDIOFOCUS_GAIN, state)
        assertEquals(FocusPolicy.FocusAction.Restore(volume = 0.35f, resume = false), action)
    }

    @Test
    fun testNoOpOnUnknownFocus() {
        val state = FocusPolicy.FocusState(wasPlaying = true, currentVolume = 1f)
        val action = FocusPolicy.decide(999, state)
        assertEquals(FocusPolicy.FocusAction.NoOp, action)
    }

    @Test
    fun testDuckVolumeConstantIsCorrect() {
        // Verify the duck volume is 5% (0.05f), well below Media3's default ~20%.
        assertEquals(0.05f, FocusPolicy.DUCK_VOLUME)
        assertTrue(
            "Duck volume should be much lower than default ~20%",
            FocusPolicy.DUCK_VOLUME < 0.2f
        )
    }
}
