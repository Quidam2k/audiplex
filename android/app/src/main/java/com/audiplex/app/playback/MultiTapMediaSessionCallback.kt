package com.audiplex.app.playback

import android.content.Intent
import android.media.AudioManager
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.KeyEvent
import androidx.annotation.OptIn
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.common.util.Util
import androidx.media3.session.MediaSession
import androidx.media3.session.SessionResult

/**
 * Headset media keys: PLAY_PAUSE counted into single / double / triple taps; [MediaKeyRules] decides.
 * #3249: discrete PLAY and PAUSE keys (and the notification's buttons, which send them) come here
 * too and act at once. A freshly connected headset sent PAUSE while we were
 * paused (11/24 18:53:58Z) and the press did nothing.
 */
@OptIn(UnstableApi::class)
class MultiTapMediaSessionCallback(
    private val player: Player,
    private val audioManager: AudioManager,
    /** #3249: ms since an audio output was added or removed; null if none yet. */
    private val msSinceRouteChange: () -> Long?,
    /** #3249: one `media_key` client log per press, so "the button didn't work" is answerable. */
    private val report: (message: String, detail: Map<String, String>) -> Unit,
) : MediaSession.Callback {

    private val handler = Handler(Looper.getMainLooper())
    private val taps = MediaKeyRules.TapCounter()

    /** Who sent the first tap of the run still open, for its log line. */
    private var runSource = Source(0, "", false)

    private data class Source(val keyCode: Int, val controller: String, val notification: Boolean)

    private val dispatchTaps = Runnable {
        taps.flush()?.let { dispatch(it, runSource) }
    }

    override fun onMediaButtonEvent(
        session: MediaSession,
        controllerInfo: MediaSession.ControllerInfo,
        intent: Intent
    ): Boolean {
        @Suppress("DEPRECATION")
        val keyEvent: KeyEvent = intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT) ?: return false

        Log.d(
            TAG,
            "key=${keyEvent.keyCode} action=${keyEvent.action} repeat=${keyEvent.repeatCount} pending=${taps.pending}"
        )

        val key = MediaKeyRules.keyOf(keyEvent.keyCode) ?: return false
        if (keyEvent.action == KeyEvent.ACTION_DOWN && keyEvent.repeatCount == 0) {
            val source = Source(
                keyEvent.keyCode,
                controllerInfo.packageName,
                session.isMediaNotificationController(controllerInfo),
            )
            if (key == MediaKeyRules.Key.PLAY_PAUSE && !taps.pending) runSource = source
            val now = taps.press(key)
            // A discrete key closed the open run: that run keeps its own source.
            now.forEach { dispatch(it, if (it.key == MediaKeyRules.Key.PLAY_PAUSE) runSource else source) }
            handler.removeCallbacks(dispatchTaps)
            if (taps.pending) handler.postDelayed(dispatchTaps, MULTI_TAP_WINDOW_MS)
        }
        return true
    }

    /**
     * #3249: name who paused through the session (lock screen, notification, a watch, another app),
     * which the pause_source log otherwise files as the anonymous "media_session".
     */
    // Deprecated in 1.4 but still called by both session stubs before each command (checked in the 1.4.1 bytecode).
    @Suppress("OVERRIDE_DEPRECATION")
    override fun onPlayerCommandRequest(
        session: MediaSession,
        controller: MediaSession.ControllerInfo,
        playerCommand: Int,
    ): Int {
        if (playerCommand == Player.COMMAND_PLAY_PAUSE && player.playWhenReady) {
            val who = when {
                session.isMediaNotificationController(controller) -> "notification"
                session.isAutoCompanionController(controller) || session.isAutomotiveController(controller) -> "auto"
                else -> controller.packageName
            }
            PlayerHooks.noteSessionCommand("session:$who")  // elapsedRealtime, the clock takePauseSource reads
        }
        return SessionResult.RESULT_SUCCESS
    }

    /** #7042: every started player's usage, content type, state and capture policy; null if unreadable. */
    private fun activePlayers(): List<MediaKeyRules.PlayerAttrs>? = try {  // #7042
        audioManager.activePlaybackConfigurations.map {
            val a = it.audioAttributes  // #7042
            MediaKeyRules.PlayerAttrs(a.usage, a.contentType, MediaKeyRules.startedFrom(it.toString()),
                if (android.os.Build.VERSION.SDK_INT >= 29) a.allowedCapturePolicy else null)  // #7042
        }
    } catch (_: Exception) { null }  // #7042 unreadable: otherMusicPlaying keeps the old isMusicActive answer

    private fun dispatch(press: MediaKeyRules.Press, source: Source) {
        val (key, taps) = press
        // Wants to play, even while focus suppresses it, so a PAUSE then still pauses.
        val playing = !Util.shouldShowPlayButton(player, /* playIfSuppressed = */ false)
        val sinceRoute = msSinceRouteChange()
        // #7042: our own speech and Talk ping no longer count; only another app's music does.
        val players = activePlayers()  // #7042
        val otherAppPlaying = !playing && audioManager.isMusicActive && MediaKeyRules.otherMusicPlaying(players)  // #7042
        val decision = MediaKeyRules.decide(key, taps, playing, sinceRoute, otherAppPlaying)
        Log.d(TAG, "dispatch key=$key taps=$taps -> $decision")
        report(
            "${key.name.lowercase()} x$taps -> ${decision.action.name.lowercase()} (${decision.why})",
            mapOf(
                "keyCode" to source.keyCode.toString(),
                "key" to key.name,
                "taps" to taps.toString(),
                "controller" to if (source.notification) "notification" else source.controller,
                "playing" to playing.toString(),
                "msSinceRouteChange" to (sinceRoute?.toString() ?: ""),
                "otherAppPlaying" to otherAppPlaying.toString(),
                "players" to (players?.joinToString(",") { "u${it.usage}/c${it.contentType}/s${it.started}/p${it.capturePolicy}" } ?: "unreadable"),  // #7042
                "action" to decision.action.name,
                "why" to decision.why,
            ),
        )
        when (decision.action) {
            // #3249: prepares after a stream error and restarts an ended queue, where a bare play() did nothing.
            MediaKeyRules.Action.PLAY -> Util.handlePlayButtonAction(player)
            MediaKeyRules.Action.PAUSE -> {
                PlayerHooks.notePause(if (source.notification) "notification" else "media_button")  // #3552
                Util.handlePauseButtonAction(player)
            }
            // #3100 / #992: double-tap = skip to next track (standard media-remote
            // semantics). Was rewind-30s; repurposed per Todd's ask. A manual
            // (non-AUTO) transition, so PlaybackManager.onMediaItemTransition already
            // logs it as an event="skip" play-stat — the DJ-taste feedback comes free.
            MediaKeyRules.Action.NEXT -> player.seekToNextMediaItem()
            MediaKeyRules.Action.FORWARD_30 -> {
                val duration = player.duration.coerceAtLeast(0L)
                val target = (player.currentPosition + SKIP_MS).coerceAtMost(duration)
                player.seekTo(target)
            }
            MediaKeyRules.Action.IGNORE -> Unit
        }
    }

    companion object {
        private const val TAG = "MultiTap"
        // Bluetooth headsets often have 200-400ms of inter-press hardware latency,
        // so a 300ms window can split a real double-tap into two singles. 500ms
        // matches the iOS multi-tap media gesture standard.
        private const val MULTI_TAP_WINDOW_MS = 500L
        private const val SKIP_MS = 30_000L
    }
}
