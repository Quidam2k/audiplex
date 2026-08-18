package com.audiplex.app.playback

import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.data.api.DjCommandAckDto
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
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
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
/** What the DJ link is currently doing, for display only. */
enum class LinkState { UNCONFIGURED, CONNECTED, OFFLINE }

/** The outcome of one command, as reported back to the server (#900). */
internal data class DispatchResult(val status: String, val detail: String = "")

/** Nothing in the command resolved — the device did not play, and says so. */
internal fun noTracks(requested: List<Int>) =
    DispatchResult("no_tracks", "resolved 0 of ${requested.size} track ids")

/**
 * OK, but say so when only some ids resolved.
 *
 * A half-loaded queue reported as a clean success is how a library gap stays
 * invisible — the DJ would think it played five tracks when it played two.
 */
internal fun partialOrOk(requested: List<Int>, resolved: Int) =
    if (resolved < requested.size)
        DispatchResult("ok", "resolved $resolved of ${requested.size} track ids")
    else DispatchResult("ok")

/** The command arrived without a field it needs — a server/app mismatch. */
internal fun badPayload(field: String) =
    DispatchResult("bad_payload", "missing $field")

/** How many recent command ids to remember for dedupe. */
private const val EXECUTED_MEMORY = 64

@Singleton
class DjCommandClient @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val settingsStore: SettingsStore,
    private val playbackManager: PlaybackManager,
    private val clientLog: ClientLogReporter,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var commandJob: Job? = null
    private var reportJob: Job? = null

    /** Command ids already executed, so an at-least-once redelivery is a
     *  no-op rather than a second skip. Bounded; insertion-ordered so the
     *  oldest id is the one evicted. */
    private val executed = linkedSetOf<Long>()

    private val _linkState = MutableStateFlow(LinkState.UNCONFIGURED)

    /**
     * Whether the command long-poll is currently reaching the server.
     *
     * Published so the DJ-link notification can say something true instead of
     * a static "running" (#3022). Read-only and observational: DjLinkService
     * watches this, and nothing here knows the service exists — which is what
     * keeps the service removable.
     */
    val linkState: StateFlow<LinkState> = _linkState.asStateFlow()

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
                _linkState.value = LinkState.UNCONFIGURED
                delay(3000) // not logged in / no server yet — wait and retry
                continue
            }
            try {
                val resp = api.getNextPlaybackCommand()
                _linkState.value = LinkState.CONNECTED
                if (resp.code() == 204) continue // long-poll timeout — re-issue
                val cmd = resp.body() ?: continue
                handle(cmd)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _linkState.value = LinkState.OFFLINE
                delay(2000) // network blip / Tailscale down — back off and retry
            }
        }
    }

    /**
     * Run one command and tell the server what happened.
     *
     * The ack is the point. Before it, a command was destroyed by the poll
     * that delivered it, so a command this client dropped — an unresolvable
     * track id, a malformed payload, a thrown exception — was indistinguishable
     * from one still in flight. Every exit path from here reports something.
     */
    private suspend fun handle(cmd: DjCommandDto) {
        if (!executed.add(cmd.id)) {
            // Delivery is at-least-once: the server re-offers a command it
            // never heard an ack for. Executing it twice would double-skip or
            // restart a track, so re-ack instead and do nothing.
            ack(cmd.id, "ok", "duplicate delivery ${cmd.deliveryCount}, already executed")
            return
        }
        while (executed.size > EXECUTED_MEMORY) {
            executed.remove(executed.first())
        }
        val result = try {
            dispatch(cmd)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            clientLog.report(
                level = "error",
                event = "command_failed",
                message = e.message ?: e.javaClass.simpleName,
                detail = mapOf("command_id" to cmd.id.toString(), "type" to cmd.type),
            )
            DispatchResult("error", e.message ?: e.javaClass.simpleName)
        }
        ack(cmd.id, result.status, result.detail)
    }

    /** Best-effort ack; a failure here must never break the command loop. */
    private suspend fun ack(commandId: Long, status: String, detail: String) {
        val api = apiHolder.api ?: return
        runCatching { api.ackPlaybackCommand(commandId, DjCommandAckDto(status, detail)) }
    }

    private suspend fun dispatch(cmd: DjCommandDto): DispatchResult {
        val baseUrl = apiHolder.baseUrl
        when (cmd.type) {
            "play_now" -> {
                val requested = cmd.payload?.trackIds.orEmpty()
                val tracks = resolveTracks(requested)
                if (tracks.isEmpty()) return noTracks(requested)
                withContext(Dispatchers.Main) {
                    playbackManager.playTracks(
                        tracks = tracks,
                        baseUrl = baseUrl,
                        title = "DJ Queue",
                        albumLookup = emptyMap(),
                    )
                }
                return partialOrOk(requested, tracks.size)
            }
            "queue" -> {
                val requested = cmd.payload?.trackIds.orEmpty()
                val tracks = resolveTracks(requested)
                if (tracks.isEmpty()) return noTracks(requested)
                withContext(Dispatchers.Main) {
                    playbackManager.enqueueTracks(tracks, baseUrl)
                }
                return partialOrOk(requested, tracks.size)
            }
            "play_next" -> {
                val requested = cmd.payload?.trackIds.orEmpty()
                val tracks = resolveTracks(requested)
                if (tracks.isEmpty()) return noTracks(requested)
                withContext(Dispatchers.Main) {
                    playbackManager.playNextTracks(tracks, baseUrl)
                }
                return partialOrOk(requested, tracks.size)
            }
            "reorder" -> {
                val from = cmd.payload?.fromIndex ?: return badPayload("from_index")
                val to = cmd.payload.toIndex ?: return badPayload("to_index")
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
                val positionMs = cmd.payload?.positionMs ?: return badPayload("position_ms")
                withContext(Dispatchers.Main) { playbackManager.seekTo(positionMs) }
            }
            "volume" -> {
                val volume = cmd.payload?.volume ?: return badPayload("volume")
                withContext(Dispatchers.Main) { playbackManager.setPlayerVolume(volume) }
            }
            "play_stream" -> {
                val url = cmd.payload?.url ?: return badPayload("url")
                val title = cmd.payload?.title ?: "Live stream"
                withContext(Dispatchers.Main) { playbackManager.playStreamUrl(url, title) }
            }
            "announce" -> {
                // DJ voice break (#431). clip_url is relative so the clip is
                // fetched from OUR configured base URL, not whatever host the
                // agent happened to reach the server on.
                val clipUrl = cmd.payload?.clipUrl ?: return badPayload("clip_url")
                val clipId = cmd.payload.clipId ?: return badPayload("clip_id")
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
            // An unknown type is a real mismatch between server and app
            // versions, and saying so beats the silence it used to get.
            else -> return DispatchResult("unknown_type", cmd.type)
        }
        return DispatchResult("ok")
    }

    /** Resolve DJ track IDs to full track metadata via the catalog API. */
    private suspend fun resolveTracks(ids: List<Int>): List<TrackSchema> {
        if (ids.isEmpty()) return emptyList()
        val api = apiHolder.api ?: return emptyList()
        return ids.mapNotNull { id -> runCatching { api.getTrack(id) }.getOrNull() }
    }

    /**
     * Post now-playing state up every 5s.
     *
     * Every tick is wrapped: before #2961 an exception here killed this job
     * outright and, because the scope uses a SupervisorJob, it died without
     * taking [commandLoop] with it or surfacing anywhere. The DJ went blind
     * while remote control kept working, which is the worst of both — so the
     * loop must survive anything a single tick can throw.
     */
    private suspend fun reportLoop() {
        var lastKey = ""
        while (coroutineContext[Job]?.isActive == true) {
            delay(5000)
            try {
                lastKey = reportOnce(lastKey)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                clientLog.report(
                    level = "error",
                    event = "report_loop_error",
                    message = e.message ?: e.javaClass.simpleName,
                )
            }
        }
    }

    /** One state report. Returns the new dedup key. */
    private suspend fun reportOnce(lastKey: String): String {
        val api = apiHolder.api ?: return lastKey
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
            // Cached snapshot, NOT controller.volume — that call is main-thread
            // only and we are on Dispatchers.IO here (#2961).
            volume = playbackManager.playerVolume(),
        )
        // Always refresh while playing (position moves); otherwise only on
        // a meaningful state change so we don't spam the server when idle.
        // Device liveness does NOT depend on this — the server tracks that
        // from the command long-poll, which beats every ~25s regardless.
        val key = "${state.playing}:${trackDto?.id}:${state.queueIndex}"
        if (playing || key != lastKey) {
            api.postPlaybackState(state)
            return key
        }
        return lastKey
    }
}
