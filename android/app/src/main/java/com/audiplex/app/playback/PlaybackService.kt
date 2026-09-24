package com.audiplex.app.playback

import android.app.PendingIntent
import android.content.Intent
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.ForwardingPlayer
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSourceBitmapLoader
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.CacheBitmapLoader
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import com.audiplex.app.MainActivity
import com.google.common.util.concurrent.MoreExecutors
import dagger.hilt.android.AndroidEntryPoint
import okhttp3.OkHttpClient
import java.util.concurrent.Executors
import javax.inject.Inject

@AndroidEntryPoint
class PlaybackService : MediaSessionService() {

    @Inject lateinit var okHttpClient: OkHttpClient

    private var mediaSession: MediaSession? = null
    private var audioFocusManager: AudioFocusManager? = null

    @OptIn(UnstableApi::class)
    override fun onCreate() {
        super.onCreate()

        val dataSourceFactory = OkHttpDataSource.Factory(okHttpClient)
        val mediaSourceFactory = DefaultMediaSourceFactory(dataSourceFactory)

        // Artwork (notification / lock screen) is fetched by the MediaSession's
        // BitmapLoader, which otherwise uses an unauthenticated HTTP data source and
        // 401s on the token-protected /cover endpoints. Route it through the same
        // authed OkHttp client used for audio.
        val bitmapLoader = CacheBitmapLoader(
            DataSourceBitmapLoader(
                MoreExecutors.listeningDecorator(Executors.newSingleThreadExecutor()),
                dataSourceFactory,
            )
        )

        val player = ExoPlayer.Builder(this)
            .setMediaSourceFactory(mediaSourceFactory)
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(C.USAGE_MEDIA)
                    // #3099/#991: default MUSIC so agent speech DUCKS the music
                    // (talk-over) instead of pausing it. PlaybackManager overrides
                    // this to SPEECH per-kind for audiobooks; FocusPolicy reads the
                    // content type on each focus change and pauses SPEECH under a
                    // can-duck loss (narrator rule) while music ducks to ~5%.
                    .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
                    .build(),
                // Media3's built-in focus handling only ducks to ~20%, which bleeds
                // into the headset mic under agent TTS. AudioFocusManager below owns
                // focus instead (request on play, abandon on user pause / destroy).
                /* handleAudioFocus = */ false
            )
            .setHandleAudioBecomingNoisy(true)
            .build()

        val focusManager = AudioFocusManager(this, player)
        audioFocusManager = focusManager
        player.addListener(object : Player.Listener {
            override fun onPlayWhenReadyChanged(playWhenReady: Boolean, reason: Int) {
                if (playWhenReady) {
                    // Every play re-requests (idempotent); denied focus means we
                    // must not start over a call or another exclusive holder.
                    if (!focusManager.requestFocus()) player.pause()
                } else if (reason == Player.PLAY_WHEN_READY_CHANGE_REASON_USER_REQUEST) {
                    focusManager.onUserPause()
                }
            }
        })

        val forwardingPlayer = object : ForwardingPlayer(player) {
            override fun getSeekForwardIncrement(): Long = 30_000L
            override fun getSeekBackIncrement(): Long = 30_000L
        }

        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        mediaSession = MediaSession.Builder(this, forwardingPlayer)
            .setSessionActivity(pendingIntent)
            .setCallback(MultiTapMediaSessionCallback(forwardingPlayer))
            .setBitmapLoader(bitmapLoader)
            .build()
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? =
        mediaSession

    override fun onTaskRemoved(rootIntent: Intent?) {
        val player = mediaSession?.player
        if (player == null || !player.playWhenReady || player.mediaItemCount == 0) {
            stopSelf()
        }
    }

    override fun onDestroy() {
        audioFocusManager?.abandonFocus()
        audioFocusManager = null
        mediaSession?.run {
            player.release()
            release()
        }
        mediaSession = null
        super.onDestroy()
    }
}
