package com.audiplex.app.playback

import android.os.SystemClock
import androidx.media3.exoplayer.ExoPlayer

/**
 * Same-process links between [PlaybackService], which owns the ExoPlayer, and
 * [PlaybackManager], which only holds a MediaController (#6913).
 *
 * - Pause source (#3552): whoever pauses notes why first; PlaybackManager's
 *   playWhenReady listener takes it and logs `pause_source` to the server, so
 *   a pause nobody admits to can be traced.
 * - Stop after this song (#3505): ExoPlayer's pauseAtEndOfMediaItems is not on
 *   the controller API, so the DJ's exact end-of-song stop goes through here.
 */
object PlayerHooks {
    @Volatile
    var exoPlayer: ExoPlayer? = null

    /** #4118: run by the service just before it releases [exoPlayer], so audio effects detach. Main thread. */
    var onPlayerReleasing: (() -> Unit)? = null

    @Volatile
    private var pauseSource: String? = null

    fun notePause(source: String) {
        pauseSource = source
    }

    @Volatile
    private var sessionSource: String? = null

    @Volatile
    private var sessionSourceAtMs = 0L

    /**
     * #3249: the session controller that asked for play/pause. It names a pause only when nobody
     * nearer has, and only briefly: a controller's play() while already playing must not label
     * some later focus pause.
     */
    fun noteSessionCommand(source: String, nowMs: Long = SystemClock.elapsedRealtime()) {
        sessionSource = source
        sessionSourceAtMs = nowMs
    }

    fun takePauseSource(nowMs: Long = SystemClock.elapsedRealtime()): String? {
        val session = sessionSource?.takeIf { nowMs - sessionSourceAtMs in 0..SESSION_SOURCE_TTL_MS }
        sessionSource = null
        return (pauseSource ?: session).also { pauseSource = null }
    }

    /** #3249: the session applies a command right after asking us, so a second is plenty. */
    const val SESSION_SOURCE_TTL_MS = 1_000L

    /** Pause when the current song ends (one-shot). Main thread. False if no player. */
    fun stopAfterCurrent(): Boolean {
        val player = exoPlayer ?: return false
        player.pauseAtEndOfMediaItems = true
        return true
    }

    /** #5836: the outro clip that must stop the player when it ends (announce then="pause"). */
    @Volatile
    var pauseAfterClip: DjClipRules.PauseAfter? = null

    /** Drop a pending stop-after-this-song. Main thread. */
    fun cancelStopAfterCurrent() {
        exoPlayer?.pauseAtEndOfMediaItems = false
    }

    /** #5695: a real focus change (a persona line, a call) took over our overlay duck: stop the clip. Main thread. */
    var onOverlayPreempted: ((String) -> Unit)? = null

    /** #7528: the service's focus manager, so a level change can reach a duck. */
    @Volatile
    var focusManager: AudioFocusManager? = null

    /**
     * #7528: while ducked (or restoring), make [volume] the level the restore
     * lands on, and return true; the caller must not set the volume itself,
     * which would un-duck a Talk. False when not ducked. Main thread.
     */
    fun retargetRestore(volume: Float): Boolean = focusManager?.retargetRestore(volume) ?: false

    /**
     * #4018: the sleep bed's state, so PlaybackService keeps the process
     * foreground while the bed plays with the book paused. The service sets
     * [onBedStateChanged] to re-run its notification update. Main thread.
     */
    @Volatile
    var bedState: BedState = BedState.Off
        private set

    var onBedStateChanged: (() -> Unit)? = null

    fun setBedState(state: BedState) {
        bedState = state
        onBedStateChanged?.invoke()
    }
}
