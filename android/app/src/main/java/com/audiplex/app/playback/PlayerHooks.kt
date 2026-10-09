package com.audiplex.app.playback

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

    @Volatile
    private var pauseSource: String? = null

    fun notePause(source: String) {
        pauseSource = source
    }

    fun takePauseSource(): String? = pauseSource.also { pauseSource = null }

    /** Pause when the current song ends (one-shot). Main thread. False if no player. */
    fun stopAfterCurrent(): Boolean {
        val player = exoPlayer ?: return false
        player.pauseAtEndOfMediaItems = true
        return true
    }

    /** Drop a pending stop-after-this-song. Main thread. */
    fun cancelStopAfterCurrent() {
        exoPlayer?.pauseAtEndOfMediaItems = false
    }

    /** #7528: the service's focus manager, so a level change can reach a duck. */
    @Volatile
    var focusManager: AudioFocusManager? = null

    /**
     * #7528: while ducked (or restoring), make [volume] the level the restore
     * lands on, and return true; the caller must not set the volume itself,
     * which would un-duck a Talk. False when not ducked. Main thread.
     */
    fun retargetRestore(volume: Float): Boolean = focusManager?.retargetRestore(volume) ?: false
}
