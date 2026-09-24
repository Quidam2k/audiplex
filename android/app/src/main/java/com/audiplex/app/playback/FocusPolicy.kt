package com.audiplex.app.playback

import android.media.AudioManager

/**
 * Pure decision logic for audio focus handling. Tests can verify behavior without
 * mocking the Android AudioFocus API.
 *
 * The policy lets music duck much deeper (~5%) than Media3's built-in ~20% when
 * transient focus is lost to Pantheon's TTS / system sounds, reducing bleed into
 * the headset mic. It also preserves the #3099/#991 rule: audiobooks (SPEECH
 * content) PAUSE under a can-duck loss instead of ducking, because a ducked
 * narrator is just lost words.
 */
object FocusPolicy {
    const val DUCK_VOLUME = 0.05f  // 5% volume on transient loss

    sealed class FocusAction {
        /** Set the player volume to [volume] (transient duck). */
        data class Duck(val volume: Float) : FocusAction()

        /** Pause; a later GAIN may resume. */
        object Pause : FocusAction()

        /** Pause and give up focus: Android sends no GAIN after AUDIOFOCUS_LOSS. */
        object PauseAndAbandon : FocusAction()

        /** Focus regained: put the volume back and resume if we were the ones who paused. */
        data class Restore(val volume: Float, val resume: Boolean) : FocusAction()

        object NoOp : FocusAction()
    }

    /**
     * @param wasPlaying   true if the player was playing when the policy paused it
     * @param currentVolume the player's volume right now
     * @param preDuckVolume the volume before the policy ducked it (null if not ducked)
     * @param isSpeech     true when the player's content type is SPEECH (audiobooks)
     */
    data class FocusState(
        val wasPlaying: Boolean = false,
        val currentVolume: Float = 1f,
        val preDuckVolume: Float? = null,
        val isSpeech: Boolean = false,
    )

    /**
     * Determine the action to take when audio focus changes.
     *
     * @param focusChange one of AudioManager.AUDIOFOCUS_*
     * @param state current player state
     * @return action to apply to the player
     */
    fun decide(focusChange: Int, state: FocusState): FocusAction = when (focusChange) {
        AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK -> {
            // Narrator rule: speech pauses, music ducks to 5%.
            if (state.isSpeech) FocusAction.Pause else FocusAction.Duck(DUCK_VOLUME)
        }
        AudioManager.AUDIOFOCUS_LOSS_TRANSIENT -> {
            // Cannot-duck loss (e.g. call): pause, resume on GAIN.
            FocusAction.Pause
        }
        AudioManager.AUDIOFOCUS_LOSS -> {
            // Another app took focus for good: pause and abandon, no auto-resume.
            FocusAction.PauseAndAbandon
        }
        AudioManager.AUDIOFOCUS_GAIN -> {
            // Restore the pre-duck dial level (never a hardcoded 1.0 — #997/#3111
            // per-kind volumes live above us) and resume only if WE paused it.
            FocusAction.Restore(
                volume = state.preDuckVolume ?: state.currentVolume,
                resume = state.wasPlaying,
            )
        }
        else -> FocusAction.NoOp
    }
}
