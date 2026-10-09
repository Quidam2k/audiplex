package com.audiplex.app.playback

import android.app.NotificationManager
import android.content.Context
import com.audiplex.app.BuildConfig
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.data.api.DjCommandAckDto
import com.audiplex.app.data.api.DjCommandDto
import com.audiplex.app.data.api.NowPlayingBookDto
import com.audiplex.app.data.api.NowPlayingTrackDto
import com.audiplex.app.data.api.PlaybackStateDto
import com.audiplex.app.data.api.QueueTrackDto
import com.audiplex.app.data.api.TrackSchema
import com.audiplex.app.data.download.DownloadRepository
import com.audiplex.app.data.download.PlaybackPositionRepository
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.dropWhile
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
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
 * resume, previous, seek, volume, play_stream, replace_upcoming, play_book. The reportLoop also publishes
 * the full queue (with indices) and the current player volume so the agent
 * can DJ with visibility and issue index-based reorders. play_stream routes
 * an external HTTP audio stream (e.g. Radio Free Luna) to the device —
 * queue ops don't apply to it, and the reportLoop reports title-only state.
 *
 * Sleep engine (#1728): bed_play/bed_stop/bed_volume control a second,
 * independent looping layer (PlaybackManager.bedPlay et al) that runs
 * alongside the main player above; sleep_timer/cancel_sleep_timer fade the
 * MAIN player out after a delay. Neither reports through reportLoop yet
 * (v1 — status is visible via dj_command_status ack, not dj_now_playing).
 */
/** What the DJ link is currently doing, for display only. */
enum class LinkState { UNCONFIGURED, CONNECTED, OFFLINE }

/** The outcome of one command, as reported back to the server (#900). */
internal data class DispatchResult(val status: String, val detail: String = "")

/** Requested ids that did not come back from the catalog, order kept (#ride0928). */
internal fun droppedIds(requested: List<Int>, resolvedIds: Collection<Int>): List<Int> =
    requested.filter { it !in resolvedIds.toSet() }

private fun droppedDetail(dropped: List<Int>) =
    "dropped ${dropped.size} unresolvable id(s): $dropped"

/**
 * Nothing in the command resolved — the device did not play, and says so.
 * #ride0928: status is "failed" and the detail names the ids, not just a count.
 */
internal fun noTracks(requested: List<Int>) =
    DispatchResult("failed", droppedDetail(requested))

/**
 * OK, but name the ids that were dropped when only some resolved.
 *
 * A half-loaded queue reported as a clean success is how a library gap stays
 * invisible — the DJ would think it played five tracks when it played two.
 * #ride0928: the detail lists the dropped ids so the DJ can fix its pool.
 */
internal fun partialOrOk(requested: List<Int>, resolvedIds: Collection<Int>): DispatchResult {
    val dropped = droppedIds(requested, resolvedIds)
    return if (dropped.isNotEmpty()) DispatchResult("ok", droppedDetail(dropped))
    else DispatchResult("ok")
}

/** The command arrived without a field it needs — a server/app mismatch. */
internal fun badPayload(field: String) =
    DispatchResult("bad_payload", "missing $field")

/** How many recent command ids to remember for dedupe. */
private const val EXECUTED_MEMORY = 64

/** #6913: commands after which the phone logs queue_synced. */
private val QUEUE_CMDS = setOf("play_now", "replace_upcoming", "queue", "play_next", "activate", "reorder")

/** How long a play_now gets to produce sound before its ack says it failed. */
private const val START_TIMEOUT_MS = 8_000L

/** Idle now-playing heartbeat, so a restarted server never shows "never". */
private const val IDLE_REPORT_INTERVAL_MS = 60_000L

/**
 * Whether a play request actually produced sound, as an ack (#2843).
 *
 * [started] is true once the player reports playing; [error] is a player
 * error raised after the request. Neither means it sat silent until timeout.
 */
internal fun startResult(
    started: Boolean,
    error: String?,
    base: DispatchResult,
    wasPlaying: Boolean = false,
    stillPlaying: Boolean = false,
): DispatchResult =
    when {
        error != null -> DispatchResult("failed", "player error: $error")
        started -> base
        // Something was already playing and never visibly stopped: the swap
        // may have been seamless, so don't call it a failure — say it's unproven.
        wasPlaying && stillPlaying -> DispatchResult(
            base.status,
            listOf(base.detail, "start not confirmed: audio never paused during the swap")
                .filter { it.isNotEmpty() }.joinToString("; "),
        )
        else -> DispatchResult("failed", "not playing ${START_TIMEOUT_MS / 1000}s after load")
    }

/**
 * The ack for a redelivered command: the ORIGINAL outcome, marked as a replay.
 * A redelivery normally means our first ack was lost, so answering "failed"
 * would misreport a command that worked. Null = no record (evicted).
 */
internal fun replayResult(original: DispatchResult?, deliveryCount: Int): DispatchResult {
    val note = "redelivery $deliveryCount, already executed"
    return if (original == null) DispatchResult("duplicate", note)
    else DispatchResult(original.status, listOf(note, original.detail).filter { it.isNotEmpty() }.joinToString("; "))
}

/** Should this tick post now-playing? Always while playing, else on change or heartbeat. */
internal fun shouldReport(playing: Boolean, key: String, lastKey: String, sinceLastMs: Long): Boolean =
    playing || key != lastKey || sinceLastMs >= IDLE_REPORT_INTERVAL_MS

/** Catalog lookups in flight at once while resolving a DJ track list (#3249). */
internal const val RESOLVE_PARALLELISM = 8

/**
 * Resolve [ids] with at most [parallelism] fetches in flight, keeping order and
 * dropping ids that fail or come back null (#3249).
 *
 * One lookup at a time is what kept a ~990-track queue busy for ~3 minutes
 * on 2026-09-28.
 */
internal suspend fun <T : Any> resolveConcurrently(
    ids: List<Int>,
    parallelism: Int = RESOLVE_PARALLELISM,
    fetch: suspend (Int) -> T?,
): List<T> = coroutineScope {
    val gate = Semaphore(parallelism)
    ids.map { id ->
        async {
            gate.withPermit {
                try {
                    fetch(id)
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    null
                }
            }
        }
    }.awaitAll().filterNotNull()
}

/**
 * Keeps polling while commands run (#3249).
 *
 * [poll] feeds an unbounded inbox; one executor drains it in arrival order, so
 * commands still run one at a time. Before this the long-poll stopped for as
 * long as a command took, and the server logged a slow queue as a link gap. A
 * redelivery of a still-running command now waits behind it and replays the
 * real outcome instead of racing it.
 */
internal class CommandPump<T : Any>(
    private val poll: suspend () -> T?,
    private val handle: suspend (T) -> Unit,
) {
    suspend fun run(): Unit = coroutineScope {
        val inbox = Channel<T>(Channel.UNLIMITED)
        launch {
            for (cmd in inbox) {
                try {
                    handle(cmd)
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    // handle() reports its own failures; never let one stop the drain.
                }
            }
        }
        try {
            while (isActive) {
                poll()?.let { inbox.send(it) }
            }
        } finally {
            inbox.close()
        }
    }
}

@Singleton
class DjCommandClient @Inject constructor(
    @ApplicationContext private val context: Context,
    private val apiHolder: ApiServiceHolder,
    private val settingsStore: SettingsStore,
    private val playbackManager: PlaybackManager,
    private val clientLog: ClientLogReporter,
    private val downloadRepository: DownloadRepository,
    private val positionRepository: PlaybackPositionRepository,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var commandJob: Job? = null
    private var reportJob: Job? = null

    /** Command ids already executed, so an at-least-once redelivery is a
     *  no-op rather than a second skip. Bounded; insertion-ordered so the
     *  oldest id is the one evicted. */
    private val executed = LinkedHashMap<Long, DispatchResult?>()

    /** Serializes now-playing posts so a slow older snapshot can't land after
     *  a newer one (the loop and the post-command report can overlap). */
    private val reportMutex = Mutex()

    /** Last now-playing post: its dedup key and when it went up. Shared by
     *  the 5s loop and the post-command report. */
    @Volatile private var lastReportKey = ""
    @Volatile private var lastReportAt = 0L

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

    /** Poll and execute separately, so a slow command never silences the link (#3249). */
    private suspend fun commandLoop() = CommandPump(::pollOnce, ::handle).run()

    /** One long-poll. Null means nothing to run (timeout, no login, or a failure after its back-off). */
    private suspend fun pollOnce(): DjCommandDto? {
        val api = apiHolder.api
        val token = runCatching { settingsStore.authToken.first() }.getOrDefault("")
        if (api == null || token.isBlank()) {
            _linkState.value = LinkState.UNCONFIGURED
            delay(3000) // not logged in / no server yet — wait and retry
            return null
        }
        return try {
            val resp = api.getNextPlaybackCommand()
            _linkState.value = LinkState.CONNECTED
            if (resp.code() == 204) null // long-poll timeout — re-issue
            else resp.body()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            _linkState.value = LinkState.OFFLINE
            delay(2000) // network blip / Tailscale down — back off and retry
            null
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
        if (executed.containsKey(cmd.id)) {
            // Delivery is at-least-once: the server re-offers a command it
            // never heard an ack for. Executing it twice would double-skip or
            // restart a track, so re-ack the ORIGINAL outcome and do nothing.
            // The detail always says it's a replay — a bare "ok" here is what
            // hid the 2026-09-27 silent phone (#2843).
            val replay = replayResult(executed[cmd.id], cmd.deliveryCount)
            ack(cmd.id, replay.status, replay.detail)
            return
        }
        executed[cmd.id] = null
        while (executed.size > EXECUTED_MEMORY) {
            executed.remove(executed.keys.first())
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
        executed[cmd.id] = result
        ack(cmd.id, result.status, result.detail)
        if (cmd.type in QUEUE_CMDS && result.status == "ok") reportQueueSynced(cmd)
        // Tell the DJ what the command did right away rather than on the next
        // tick; best-effort, like the loop.
        runCatching { reportOnce(force = true) }
    }

    /**
     * Wait until the player is actually playing, or a NEW player error fires,
     * or [START_TIMEOUT_MS] passes, and turn that into the ack. "ok" used to
     * mean "handed to the player", which is how a silent phone acked every
     * command it swallowed (#2843).
     */
    private suspend fun awaitStart(
        errorSeqBefore: Long,
        wasPlaying: Boolean,
        base: DispatchResult,
    ): DispatchResult {
        val outcome = withTimeoutOrNull(START_TIMEOUT_MS) {
            val signals = combine(playbackManager.isPlaying, playbackManager.lastPlayerError) { playing, err ->
                playing to err?.takeIf { it.seq > errorSeqBefore }?.message
            }
            // If the old song was playing, its isPlaying=true is stale: wait
            // for the swap to stop it (buffering) before a "true" counts.
            val fresh = if (wasPlaying) {
                var sawStop = false
                signals.dropWhile { (playing, err) ->
                    if (!playing) sawStop = true
                    err == null && !sawStop
                }
            } else signals
            fresh.first { (playing, err) -> playing || err != null }
        }
        // #3505: every DJ start (play_now, activate, ...) checks that the media
        // notification (Todd's only way to stop it off-app) actually appeared.
        if (outcome?.first == true && outcome.second == null) scheduleNotificationCheck()
        return startResult(
            started = outcome?.first == true && outcome.second == null,
            error = outcome?.second,
            base = base,
            wasPlaying = wasPlaying,
            stillPlaying = playbackManager.isPlaying.value,
        )
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
                val errorSeq = playbackManager.lastPlayerError.value?.seq ?: 0L
                val wasPlaying = playbackManager.isPlaying.value
                withContext(Dispatchers.Main) {
                    playbackManager.playTracks(
                        tracks = tracks,
                        baseUrl = baseUrl,
                        title = "DJ Queue",
                        albumLookup = emptyMap(),
                    )
                }
                return awaitStart(errorSeq, wasPlaying, partialOrOk(requested, tracks.map { it.id }))
            }
            // DJ mix (#2842): keep the current song, replace everything after
            // it. An empty list is legitimate: it trims the tail.
            "replace_upcoming" -> {
                val requested = cmd.payload?.trackIds.orEmpty()
                val tracks = resolveTracks(requested)
                if (requested.isNotEmpty() && tracks.isEmpty()) return noTracks(requested)
                // Only a music queue has a "rest of the queue". An audiobook or
                // stream is left alone; the DJ starts a mix with play_now.
                val kind = playbackManager.playerKind.value
                if (kind != null && kind != PlayerKind.Music) {
                    return DispatchResult("no_music_queue", "current player: $kind")
                }
                withContext(Dispatchers.Main) {
                    playbackManager.replaceUpcoming(tracks, baseUrl)
                }
                return partialOrOk(requested, tracks.map { it.id })
            }
            "queue" -> {
                val requested = cmd.payload?.trackIds.orEmpty()
                val tracks = resolveTracks(requested)
                if (tracks.isEmpty()) return noTracks(requested)
                withContext(Dispatchers.Main) {
                    playbackManager.enqueueTracks(tracks, baseUrl)
                }
                return partialOrOk(requested, tracks.map { it.id })
            }
            "play_next" -> {
                val requested = cmd.payload?.trackIds.orEmpty()
                val tracks = resolveTracks(requested)
                if (tracks.isEmpty()) return noTracks(requested)
                withContext(Dispatchers.Main) {
                    playbackManager.playNextTracks(tracks, baseUrl)
                }
                return partialOrOk(requested, tracks.map { it.id })
            }
            "reorder" -> {
                val from = cmd.payload?.fromIndex ?: return badPayload("from_index")
                val to = cmd.payload.toIndex ?: return badPayload("to_index")
                withContext(Dispatchers.Main) {
                    playbackManager.moveTrack(from, to)
                }
            }
            // Transfer handshake (#2021). deactivate: playback is moving to
            // another device, so stop here and report exactly where we were;
            // the server hands off to the new device when we ack. activate: we
            // are the new device, so resume the old one's queue at its spot.
            "deactivate" -> {
                withContext(Dispatchers.Main) { playbackManager.pause("transfer #${cmd.id}") }
                runCatching { reportOnce(force = true) }
            }
            "activate" -> {
                val requested = cmd.payload?.trackIds.orEmpty()
                // Nothing was playing on the old device: we're just the target now.
                if (requested.isEmpty()) return DispatchResult("ok")
                val tracks = resolveTracks(requested)
                if (tracks.isEmpty()) return noTracks(requested)
                val resumes = tracks.first().id == requested.first()
                val startMs = if (resumes) cmd.payload?.positionMs ?: 0L else 0L
                val errorSeq = playbackManager.lastPlayerError.value?.seq ?: 0L
                val wasPlaying = playbackManager.isPlaying.value
                withContext(Dispatchers.Main) {
                    playbackManager.playTracks(
                        tracks = tracks,
                        baseUrl = baseUrl,
                        title = "DJ Queue",
                        albumLookup = emptyMap(),
                        startPositionMs = startMs,
                        // #3601: a paused handoff/restore loads without ever
                        // starting, rather than play-then-pause (a blip of sound).
                        play = cmd.payload?.playing != false,
                    )
                }
                // A handoff that arrives paused is meant to stay silent.
                if (cmd.payload?.playing == false) return partialOrOk(requested, tracks.map { it.id })
                return awaitStart(errorSeq, wasPlaying, partialOrOk(requested, tracks.map { it.id }))
            }
            "skip" -> {
                // Advance to the next track in the queue. For music this maps to
                // seekToNextMediaItem (an existing Media3 op — zero new queue ops).
                withContext(Dispatchers.Main) {
                    playbackManager.skipForward()
                }
            }
            "pause" -> withContext(Dispatchers.Main) { playbackManager.pause("dj_command #${cmd.id}") }
            // #3505: pause exactly when the current song ends (one-shot).
            "stop_after_current" -> {
                val armed = withContext(Dispatchers.Main) { playbackManager.stopAfterCurrent() }
                if (!armed) return DispatchResult("failed", "no player")
            }
            "cancel_stop_after_current" -> withContext(Dispatchers.Main) { playbackManager.cancelStopAfterCurrent() }
            // #3713: start an audiobook by voice, like play_now for tracks.
            // No position_ms means "where Todd left off" (the Continue button's
            // reconciler, so local and server progress both count).
            "play_book" -> {
                val bookId = cmd.payload?.bookId ?: return badPayload("book_id")
                val book = runCatching { apiHolder.api?.getBook(bookId) }.getOrNull()
                    ?: downloadRepository.getCachedBookDetail(bookId)
                    ?: return DispatchResult("no_book", "book $bookId")
                val startSeconds = cmd.payload.positionMs?.let { it / 1000.0 }
                    ?: positionRepository.resolveStartPosition(bookId).positionSeconds
                val errorSeq = playbackManager.lastPlayerError.value?.seq ?: 0L
                val wasPlaying = playbackManager.isPlaying.value
                withContext(Dispatchers.Main) { playbackManager.play(book, baseUrl, startSeconds) }
                return awaitStart(errorSeq, wasPlaying, DispatchResult("ok", "book $bookId at ${startSeconds.toLong()} s"))
            }
            "resume" -> withContext(Dispatchers.Main) { playbackManager.resume() }
            "previous" -> withContext(Dispatchers.Main) { playbackManager.skipBack() }
            "seek" -> {
                val positionMs = cmd.payload?.positionMs ?: return badPayload("position_ms")
                withContext(Dispatchers.Main) { playbackManager.seekTo(positionMs) }
            }
            "volume" -> {
                val volume = cmd.payload?.volume ?: return badPayload("volume")
                playbackManager.setChannelVolume(volume)  // #7528: the saved dial, not the raw player
            }
            "play_stream" -> {
                val url = cmd.payload?.url ?: return badPayload("url")
                val title = cmd.payload?.title ?: "Live stream"
                withContext(Dispatchers.Main) { playbackManager.playStreamUrl(url, title) }
            }
            // Sleep engine (#1728): a second, independent looping layer plus a
            // fade-out timer on the main player. Purely additive — none of the
            // cases above are touched.
            "bed_play" -> {
                val url = cmd.payload?.url ?: return badPayload("url")
                val volume = cmd.payload.volume ?: 0.5f
                // #3367: a relative url (a library book) resolves against OUR base URL.
                val absolute = SleepFade.resolveUrl(url, baseUrl)
                withContext(Dispatchers.Main) { playbackManager.bedPlay(absolute, volume) }
            }
            "bed_stop" -> withContext(Dispatchers.Main) { playbackManager.bedStop() }
            "bed_volume" -> {
                val volume = cmd.payload?.volume ?: return badPayload("volume")
                withContext(Dispatchers.Main) { playbackManager.bedVolume(volume) }
            }
            "sleep_timer" -> {
                val minutes = cmd.payload?.minutes ?: return badPayload("minutes")
                val fadeSeconds = cmd.payload.fadeSeconds ?: 120
                val bedFadeTo = cmd.payload.bedFadeTo
                withContext(Dispatchers.Main) { playbackManager.startSleepTimer(minutes, fadeSeconds, bedFadeTo) }
            }
            "cancel_sleep_timer" -> withContext(Dispatchers.Main) { playbackManager.cancelSleepTimer() }
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

    /**
     * #6913: after a DJ queue command lands, say what the phone's queue now is,
     * so the DJ's view (dj_queue) can be checked against what Todd sees.
     */
    private fun reportQueueSynced(cmd: DjCommandDto) {
        val music = playbackManager.currentMusic.value ?: return
        val next = music.items.drop(music.currentIndex + 1).take(5).map { it.track.id }
        clientLog.report(
            level = "info",
            event = "queue_synced",
            message = "${cmd.type} #${cmd.id}: ${music.items.size} in queue, at ${music.currentIndex}",
            detail = mapOf(
                "command_id" to cmd.id.toString(),
                "type" to cmd.type,
                "length" to music.items.size.toString(),
                "current_index" to music.currentIndex.toString(),
                "current_id" to (music.items.getOrNull(music.currentIndex)?.track?.id?.toString() ?: ""),
                "next_ids" to next.joinToString(","),
                "origin" to (music.origin ?: ""),
            ),
        )
    }

    /** Resolve DJ track IDs to full track metadata via the catalog API. */
    private suspend fun resolveTracks(ids: List<Int>): List<TrackSchema> {
        if (ids.isEmpty()) return emptyList()
        val api = apiHolder.api ?: return emptyList()
        return resolveConcurrently(ids) { id -> api.getTrack(id) }
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
        while (coroutineContext[Job]?.isActive == true) {
            delay(5000)
            try {
                reportOnce()
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

    /** One state report, unless nothing changed and the heartbeat isn't due. */
    private suspend fun reportOnce(force: Boolean = false) = reportMutex.withLock {
        reportOnceLocked(force)
    }

    private suspend fun reportOnceLocked(force: Boolean) {
        val api = apiHolder.api ?: return
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
            appVersionName = BuildConfig.VERSION_NAME,
            appVersionCode = BuildConfig.VERSION_CODE,
            queueOrigin = music?.origin,  // #3601
            book = playbackManager.currentBook.value
                ?.takeIf { playbackManager.playerKind.value == PlayerKind.Audiobook }
                ?.let { NowPlayingBookDto(it.id, it.title) },
        )
        // Always refresh while playing (position moves); otherwise on a
        // meaningful state change, plus a slow idle heartbeat. Without it a
        // server restart left now-playing at "never" until something changed
        // (#2843). Device liveness does NOT depend on this — the server
        // tracks that from the command long-poll, every ~25s regardless.
        val key = "${state.playing}:${trackDto?.id}:${state.queueIndex}:${state.book?.id}"
        val now = System.currentTimeMillis()
        if (force || shouldReport(playing, key, lastReportKey, now - lastReportAt)) {
            api.postPlaybackState(state)
            lastReportKey = key
            lastReportAt = now
        }
    }

    /**
     * Schedule a check for PlaybackService notification 3 seconds after a DJ playback command (#3505).
     * If the service is not in the foreground (no notification), report a diagnostic event.
     */
    private fun scheduleNotificationCheck() {
        scope.launch {
            delay(3000)
            if (!hasMediaNotification()) {
                clientLog.report(
                    level = "error",
                    event = "no_media_notification",
                    message = "PlaybackService not in foreground 3s after DJ play command",
                    detail = mapOf(
                        "isPlaying" to playbackManager.isPlaying.value.toString()
                    ),
                )
            }
        }
    }

    /**
     * Check if PlaybackService has posted a notification (is in foreground).
     */
    private fun hasMediaNotification(): Boolean {
        val notificationManager = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
            ?: return false
        // The media notification is the one carrying a MediaSession token
        // (Media3's MediaStyle sets EXTRA_MEDIA_SESSION). "Any ongoing
        // notification" would always pass: DjLinkService keeps its own
        // foreground notification up the whole time.
        val activeNotifications = runCatching { notificationManager.activeNotifications }
            .getOrNull() ?: return false
        return activeNotifications.any {
            it.notification.extras?.containsKey(android.app.Notification.EXTRA_MEDIA_SESSION) == true
        }
    }
}
