package com.audiplex.app.playback

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Build
import androidx.media3.common.C
import androidx.media3.common.Player

/**
 * Manages audio focus with deeper ducking to prevent microphone bleed during
 * transient focus loss (e.g. agent TTS, system sounds, calls).
 *
 * Replaces Media3's `handleAudioFocus=true` with a custom request + listener so
 * music ducks to ~5% instead of the default ~20%. Decisions are made by
 * [FocusPolicy]; this class only holds state and talks to the player.
 *
 * Lifecycle (driven by [PlaybackService]):
 *  - every play → [requestFocus] (idempotent; also recovers from a prior abandon)
 *  - user pause → [onUserPause] (abandon unless the pause was ours)
 *  - service destroy → [abandonFocus]
 */
class AudioFocusManager(
    context: Context,
    private val player: Player,
) {
    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var focusRequest: AudioFocusRequest? = null
    private var hasFocus = false

    /** Volume before we ducked; null when not ducked. */
    private var preDuckVolume: Float? = null

    /** True if the policy paused the player (so a GAIN should resume it). */
    private var pausedByFocus = false
    private var wasPlaying = false

    private val focusListener = AudioManager.OnAudioFocusChangeListener { focusChange ->
        val state = FocusPolicy.FocusState(
            wasPlaying = wasPlaying,
            currentVolume = player.volume,
            preDuckVolume = preDuckVolume,
            isSpeech = player.audioAttributes.contentType == C.AUDIO_CONTENT_TYPE_SPEECH,
        )
        applyAction(FocusPolicy.decide(focusChange, state))
    }

    /**
     * Request audio focus for playback (USAGE_MEDIA, AUDIOFOCUS_GAIN).
     * Safe to call on every play; a request while already holding focus is a no-op
     * on the system side and re-arms us after an abandon.
     *
     * @return true if focus was granted
     */
    fun requestFocus(): Boolean {
        val result = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val request = focusRequest ?: AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                        .build()
                )
                .setWillPauseWhenDucked(false)  // policy decides duck-vs-pause per content type
                .setOnAudioFocusChangeListener(focusListener)
                .build()
                .also { focusRequest = it }
            audioManager.requestAudioFocus(request)
        } else {
            @Suppress("DEPRECATION")
            audioManager.requestAudioFocus(
                focusListener, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN
            )
        }
        hasFocus = result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
        return hasFocus
    }

    /**
     * The user (or a controller) paused playback. Give up focus so other apps
     * stop ducking around us — unless the pause was ours, in which case we still
     * want the GAIN that will resume it.
     */
    fun onUserPause() {
        if (!pausedByFocus) abandonFocus()
    }

    /**
     * Abandon audio focus and clear all transient state. Call when playback ends
     * or the service is released.
     */
    fun abandonFocus() {
        if (hasFocus) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                focusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
            } else {
                @Suppress("DEPRECATION")
                audioManager.abandonAudioFocus(focusListener)
            }
        }
        hasFocus = false
        preDuckVolume = null
        pausedByFocus = false
        wasPlaying = false
    }

    private fun applyAction(action: FocusPolicy.FocusAction) {
        when (action) {
            is FocusPolicy.FocusAction.Duck -> {
                // Only capture the dial level on the first duck; repeated CAN_DUCK
                // events while already ducked must not overwrite it with 0.05.
                if (preDuckVolume == null) preDuckVolume = player.volume
                player.volume = action.volume
            }
            FocusPolicy.FocusAction.Pause -> {
                wasPlaying = player.isPlaying
                pausedByFocus = true
                player.pause()
            }
            FocusPolicy.FocusAction.PauseAndAbandon -> {
                player.pause()
                abandonFocus()
            }
            is FocusPolicy.FocusAction.Restore -> {
                player.volume = action.volume
                if (action.resume && !player.isPlaying) player.play()
                preDuckVolume = null
                wasPlaying = false
                pausedByFocus = false
            }
            FocusPolicy.FocusAction.NoOp -> Unit
        }
    }
}
