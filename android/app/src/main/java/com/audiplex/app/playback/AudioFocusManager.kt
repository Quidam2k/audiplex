package com.audiplex.app.playback

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
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
private const val TAG = "AudioFocus"  // #5834

class AudioFocusManager(
    context: Context,
    private val player: Player,
    /** #5834: fade timings, so the measured fade shows in the server's client log. */
    private val log: (String) -> Unit = { Log.i(TAG, it) },
) {
    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var focusRequest: AudioFocusRequest? = null
    private var hasFocus = false

    /** True if the policy paused the player (so a GAIN should resume it). */
    @Volatile private var pausedByFocus = false
    @Volatile private var wasPlaying = false

    /** #5614: a focus loss is holding playback that the next GAIN will start. */
    val awaitingFocusResume: Boolean get() = pausedByFocus && wasPlaying

    /** #5837 F: ducked under another app's audio (a persona line). */
    val isDucked: Boolean get() = fader.preDuckVolume != null

    /** #5837 F: a DJ clip waiting paused for that app's GAIN (rules in [ClipHold]). */
    private val holdHandler = Handler(Looper.getMainLooper())
    private val clipHold = ClipHold(
        port = object : ClipHold.Port {
            override val ducked: Boolean get() = isDucked
            override val playWhenReady: Boolean get() = player.playWhenReady
            override fun markOurPause() {
                pausedByFocus = true
                wasPlaying = true
                PlayerHooks.notePause("dj_clip_hold")  // #3552
            }
            override fun clearOurPause() {
                pausedByFocus = false
                wasPlaying = false
            }
            override fun pause() = player.pause()
            override fun play() = player.play()
            override fun log(message: String) = this@AudioFocusManager.log(message)
        },
        scheduler = object : FocusFader.Scheduler {
            override fun postDelayed(delayMs: Long, block: () -> Unit) {
                holdHandler.postDelayed(block, delayMs)
            }
            override fun cancelAll() = holdHandler.removeCallbacksAndMessages(null)
        },
    )

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
            preDuckVolume = fader.restoreTarget,  // #5834 mid-fade: the dial level, not the faded one
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
        if (fader.isFading) {  // #5834 the user paused mid-fade: theirs, not ours (ours fires after the fade)
            fader.cancelRamp()  // lands on the dial level; the player is already paused
            pausedByFocus = false
            wasPlaying = false  // a later GAIN must not resume what the user paused
        }
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
        clipHold.reset()  // #5837
        fader.reset()  // #3597
        pausedByFocus = false
        wasPlaying = false
    }

    /** #5837 F: a media item is starting; hold it if it is a DJ clip under another app's duck. Main thread. */
    fun holdClipWhileDucked(mediaId: String?): Boolean = clipHold.onItemStart(mediaId)

    /** #5837 F: any play (the GAIN's resume, or Todd's tap) ends a hold. Main thread. */
    fun onPlayRequested() = clipHold.onPlayRequested()

    /**
     * #5836: the outro's then="pause" stop did not happen at the clip's end and the next
     * song started: fade out over [durationMs] and pause. A real stop, so focus is
     * abandoned as for a user pause. Main thread.
     */
    fun fadeOutAndStop(durationMs: Long) {
        val t0 = SystemClock.elapsedRealtime()
        fader.fadeOutAndPause(durationMs) {
            log("#5836 outro stop: faded out in ${SystemClock.elapsedRealtime() - t0} ms")
            PlayerHooks.notePause("dj_then_pause")
        }
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
                wasPlaying = FocusPolicy.resumeAfterPause(  // #5614
                    pausedByFocus, wasPlaying, player.isPlaying, player.playWhenReady)
                pausedByFocus = true
                val t0 = SystemClock.elapsedRealtime()  // #5834 measured on the device log
                fader.fadeOutAndPause {  // #3552 #5834 500 ms fade, then pause
                    log("#5834 focus pause: faded out in ${SystemClock.elapsedRealtime() - t0} ms")
                    PlayerHooks.notePause("audio_focus")
                }
            }
            FocusPolicy.FocusAction.PauseAndAbandon -> {
                PlayerHooks.notePause("audio_focus_lost")  // #3552
                player.pause()
                abandonFocus()
            }
            is FocusPolicy.FocusAction.Restore -> {
                val t0 = SystemClock.elapsedRealtime()  // #5834
                val fromSilence = action.resume && !player.isPlaying  // #5834
                fader.restore(action.volume, action.resume) {  // #3597 Pantheon #2187
                    if (fromSilence) log("#5834 focus resume: faded in to ${action.volume} in ${SystemClock.elapsedRealtime() - t0} ms")
                }
                wasPlaying = false
                pausedByFocus = false
            }
            FocusPolicy.FocusAction.NoOp -> Unit
        }
    }
}
