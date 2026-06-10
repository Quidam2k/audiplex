package com.audiplex.app.playback

import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.KeyEvent
import androidx.annotation.OptIn
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.session.MediaSession

@OptIn(UnstableApi::class)
class MultiTapMediaSessionCallback(
    private val player: Player
) : MediaSession.Callback {

    private val handler = Handler(Looper.getMainLooper())
    private var tapCount = 0

    private val dispatchTaps = Runnable {
        val taps = tapCount
        tapCount = 0
        dispatch(taps)
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
            "key=${keyEvent.keyCode} action=${keyEvent.action} repeat=${keyEvent.repeatCount} tapCount=$tapCount"
        )

        return when (keyEvent.keyCode) {
            KeyEvent.KEYCODE_HEADSETHOOK,
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE -> {
                if (keyEvent.action == KeyEvent.ACTION_DOWN && keyEvent.repeatCount == 0) {
                    tapCount++
                    handler.removeCallbacks(dispatchTaps)
                    handler.postDelayed(dispatchTaps, MULTI_TAP_WINDOW_MS)
                }
                true
            }
            else -> false
        }
    }

    private fun dispatch(taps: Int) {
        Log.d(TAG, "dispatch taps=$taps")
        when (taps) {
            1 -> if (player.isPlaying) player.pause() else player.play()
            2 -> {
                val target = (player.currentPosition - SKIP_MS).coerceAtLeast(0L)
                player.seekTo(target)
            }
            3 -> {
                val duration = player.duration.coerceAtLeast(0L)
                val target = (player.currentPosition + SKIP_MS).coerceAtMost(duration)
                player.seekTo(target)
            }
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
