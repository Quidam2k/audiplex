package com.audiplex.app.playback

import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.data.api.DjCommandDto
import com.audiplex.app.data.api.NowPlayingTrackDto
import com.audiplex.app.data.api.PlaybackStateDto
import com.audiplex.app.data.api.QueueTrackDto
import com.audiplex.app.data.api.TrackSchema
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import javax.inject.Inject
import javax.inject.Singleton
import kotlin.coroutines.coroutineContext

/**
 * The DJ remote-control bridge on the client side.
 *
 * Two long-lived loops, both hosted in the app-process scope (alive as long
 * as the process is — which, while playing, the foreground MediaSessionService
 * guarantees, so commands execute screen-off during playback):
 *
 *  - [commandLoop]: long-polls GET /api/playback/command/next and dispatches
 *    each command to [PlaybackManager] via the existing playback entry points.
 *  - [reportLoop]: periodically POSTs now-playing state up so the agent can
 *    see what's playing via dj_now_playing.
 *
 * Handles command types: play_now, skip, queue, play_next, reorder, pause,
 * resume, previous, seek, volume, play_stream. The reportLoop also publishes
 * the full queue (with indices) and the current player volume so the agent
 * can DJ with visibility and issue index-based reorders. play_stream routes
 * an external HTTP audio stream (e.g. Radio Free Luna) to the device —
 * queue ops don't apply to it, and the reportLoop reports title-only state.
 */
@Singleton
class DjCommandClient @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val settingsStore: SettingsStore,
    private val playbackManager: PlaybackManager,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var commandJob: Job? = null
    private var reportJob: Job? = null

    fun start() {
        if (commandJob?.isActive != true) {
            commandJob = scope.launch { commandLoop() }
        }
        if (reportJob?.isActive != true) {
            reportJob = scope.launch { reportLoop() }
        }
    }

    fun stop() {
        commandJob?.cancel(); commandJob = null
        reportJob?.cancel(); reportJob = null
    }

    private suspend fun commandLoop() {
        while (coroutineContext[Job]?.isActive == true) {
            val api = apiHolder.api
            val token = runCatching { settingsStore.authToken.first() }.getOrDefault("")
            if (api == null || token.isBlank()) {
                delay(3000) // not logged in / no server yet — wait and retry
                continue
            }
            try {
                val resp = api.getNextPlaybackCommand()
                if (resp.code() == 204) continue // long-poll timeout — re-issue
                val cmd = resp.body() ?: continue
                dispatch(cmd)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                delay(2000) // network blip / Tailscale down — back off and retry
            }
        }
    }

    private suspend fun dispatch(cmd: DjCommandDto) {
        val baseUrl = apiHolder.baseUrl
        when (cmd.type) {
            "play_now" -> {
                val tracks = resolveTracks(cmd.payload?.trackIds.orEmpty())
                if (tracks.isEmpty()) return
                withContext(Dispatchers.Main) {
                    playbackManager.playTracks(
                        tracks = tracks,
                        baseUrl = baseUrl,
                        title = "DJ Queue",
                        albumLookup = emptyMap(),
                    )
                }
            }
            "queue" -> {
                val tracks = resolveTracks(cmd.payload?.trackIds.orEmpty())
                if (tracks.isEmpty()) return
                withContext(Dispatchers.Main) {
                    playbackManager.enqueueTracks(tracks, baseUrl)
                }
            }
            "play_next" -> {
                val tracks = resolveTracks(cmd.payload?.trackIds.orEmpty())
                if (tracks.isEmpty()) return
                withContext(Dispatchers.Main) {
                    playbackManager.playNextTracks(tracks, baseUrl)
                }
            }
            "reorder" -> {
                val from = cmd.payload?.fromIndex ?: return
                val to = cmd.payload?.toIndex ?: return
                withContext(Dispatchers.Main) {
                    playbackManager.moveTrack(from, to)
                }
            }
            "skip" -> {
                // Advance to the next track in the queue. For music this maps to
                // seekToNextMediaItem (an existing Media3 op — zero new queue ops).
                withContext(Dispatchers.Main) {
                    playbackManager.skipForward()
                }
            }
            "pause" -> withContext(Dispatchers.Main) { playbackManager.pause() }
            "resume" -> withContext(Dispatchers.Main) { playbackManager.resume() }
            "previous" -> withContext(Dispatchers.Main) { playbackManager.skipBack() }
            "seek" -> {
                val positionMs = cmd.payload?.positionMs ?: return
                withContext(Dispatchers.Main) { playbackManager.seekTo(positionMs) }
            }
            "volume" -> {
                val volume = cmd.payload?.volume ?: return
                withContext(Dispatchers.Main) { playbackManager.setPlayerVolume(volume) }
            }
            "play_stream" -> {
                val url = cmd.payload?.url ?: return
                val title = cmd.payload?.title ?: "Live stream"
                withContext(Dispatchers.Main) { playbackManager.playStreamUrl(url, title) }
            }
            "announce" -> {
                // DJ voice break (#431). clip_url is relative so the clip is
                // fetched from OUR configured base URL, not whatever host the
                // agent happened to reach the server on.
                val clipUrl = cmd.payload?.clipUrl ?: return
                val clipId = cmd.payload.clipId ?: return
                val absolute = if (clipUrl.startsWith("http"))
                    clipUrl
                else
                    baseUrl.trimEnd('/') + clipUrl
                val title = cmd.payload.title ?: "DJ break"
                withContext(Dispatchers.Main) {
                    playbackManager.insertVoiceClip(
                        clipUrl = absolute,
                        clipId = clipId,
                        title = title,
                        durationSeconds = cmd.payload.durationSeconds,
                        playNow = cmd.payload.mode == "now"
                    )
                }
            }
            else -> Unit // unknown command type — ignored
        }
    }

    /** Resolve DJ track IDs to full track metadata via the catalog API. */
    private suspend fun resolveTracks(ids: List<Int>): List<TrackSchema> {
        if (ids.isEmpty()) return emptyList()
        val api = apiHolder.api ?: return emptyList()
        return ids.mapNotNull { id -> runCatching { api.getTrack(id) }.getOrNull() }
    }

    private suspend fun reportLoop() {
        var lastKey = ""
        while (coroutineContext[Job]?.isActive == true) {
            delay(5000)
            val api = apiHolder.api ?: continue
            val music = playbackManager.currentMusic.value
            val playing = playbackManager.isPlaying.value
            val streamTitle = playbackManager.currentStreamTitle.value
            val track = music?.items?.getOrNull(music.currentIndex)?.track
            // A stream has no track id/queue — report title only (id -1 is a
            // client-side sentinel; the agent only reads title/artist for it).
            val trackDto = if (streamTitle != null) {
                NowPlayingTrackDto(-1, streamTitle, "Radio Free Luna")
            } else {
                track?.let { NowPlayingTrackDto(it.id, it.title, it.artistName) }
            }
            val queue = music?.items?.mapIndexed { i, item ->
                QueueTrackDto(index = i, id = item.track.id, title = item.track.title, artist = item.track.artistName)
            } ?: emptyList()
            val state = PlaybackStateDto(
                playing = playing,
                track = trackDto,
                positionMs = playbackManager.positionMs.value,
                durationMs = playbackManager.durationMs.value,
                queueLength = music?.items?.size ?: 0,
                queueIndex = music?.currentIndex ?: 0,
                queue = queue,
                volume = playbackManager.playerVolume(),
            )
            // Always refresh while playing (position moves); otherwise only on
            // a meaningful state change so we don't spam the server when idle.
            val key = "${state.playing}:${trackDto?.id}:${state.queueIndex}"
            if (playing || key != lastKey) {
                lastKey = key
                runCatching { api.postPlaybackState(state) }
            }
        }
    }
}
