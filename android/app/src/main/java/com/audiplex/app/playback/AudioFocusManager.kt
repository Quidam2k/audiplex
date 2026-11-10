package com.audiplex.app.playback

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Build
import android.os.Handler
import android.os.Looper
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

    /** True if the policy paused the player (so a GAIN should resume it). */
    @Volatile private var pausedByFocus = false
    @Volatile private var wasPlaying = false

    /** #5614: a focus loss is holding playback that the next GAIN will start. */
    val awaitingFocusResume: Boolean get() = pausedByFocus && wasPlaying

    // #3597 duck/restore ramps, now in FocusFader (#5834). Focus callbacks and player
    // calls are on the main looper, so the ramp steps post there too.
    private val rampHandler = Handler(Looper.getMainLooper())
    private val fader = FocusFader(
        port = object : FocusFader.Port {
            override var volume: Float
                get() = player.volume
                set(value) { player.volume = value }
            override val isPlaying: Boolean get() = player.isPlaying
            override fun pause() = player.pause()
            override fun play() = player.play()
        },
        scheduler = object : FocusFader.Scheduler {
            override fun postDelayed(delayMs: Long, block: () -> Unit) {
                rampHandler.postDelayed(block, delayMs)
            }
            override fun cancelAll() = rampHandler.removeCallbacksAndMessages(null)
        },
    )

    private val focusListener = AudioManager.OnAudioFocusChangeListener { focusChange ->
        val state = FocusPolicy.FocusState(
            wasPlaying = wasPlaying,
            currentVolume = player.volume,
            preDuckVolume = fader.preDuckVolume,
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
        fader.reset()  // #3597
        pausedByFocus = false
        wasPlaying = false
    }

    /**
     * #7528: a new dial/track level arrived. Ducked: remember it as the restore
     * level. Mid-restore: re-aim the rising ramp at it. Returns false when
     * neither, so the caller sets the volume as usual.
     */
    fun retargetRestore(volume: Float): Boolean = fader.retarget(volume)

    private fun applyAction(action: FocusPolicy.FocusAction) {
        when (action) {
            is FocusPolicy.FocusAction.Duck -> fader.duck(action.volume)
            FocusPolicy.FocusAction.Pause -> {
                fader.cancelRamp()  // #3597
                wasPlaying = FocusPolicy.resumeAfterPause(  // #5614
                    pausedByFocus, wasPlaying, player.isPlaying, player.playWhenReady)
                pausedByFocus = true
                PlayerHooks.notePause("audio_focus")  // #3552
                player.pause()
            }
            FocusPolicy.FocusAction.PauseAndAbandon -> {
                PlayerHooks.notePause("audio_focus_lost")  // #3552
                player.pause()
                abandonFocus()
            }
            is FocusPolicy.FocusAction.Restore -> {
                fader.restore(action.volume, action.resume)  // #3597 Pantheon #2187
                wasPlaying = false
                pausedByFocus = false
            }
            FocusPolicy.FocusAction.NoOp -> Unit
        }
    }
}
