package com.audiplex.app.playback

import android.content.ComponentName
import android.content.Context
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.Player.DiscontinuityReason
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.api.AlbumDetail
import com.audiplex.app.data.api.AudiplexApi
import com.audiplex.app.data.download.DownloadRepository
import com.audiplex.app.data.download.MeditationStore
import com.audiplex.app.data.api.BookDetail
import com.audiplex.app.data.api.PlayStatEvent
import com.audiplex.app.data.api.PlaylistDetail
import com.audiplex.app.data.api.ProgressUpdate
import com.audiplex.app.data.api.TrackSchema
import com.audiplex.app.data.db.PlaybackPositionDao
import com.audiplex.app.data.db.PlaybackPositionEntity
import com.google.common.util.concurrent.ListenableFuture
import com.google.common.util.concurrent.MoreExecutors
import dagger.hilt.android.qualifiers.ApplicationContext
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject
import javax.inject.Singleton

enum class PlayerKind { Audiobook, Music, Stream }

data class MusicQueueItem(
    val track: TrackSchema,
    val albumId: Int,
    val albumTitle: String,
    val albumHasCover: Boolean
)

data class MusicQueueState(
    val items: List<MusicQueueItem>,
    val albumId: Int?,
    val playlistId: Int?,
    val title: String,
    val currentIndex: Int = 0
)

@Singleton
class PlaybackManager @Inject constructor(
    @ApplicationContext private val context: Context,
    private val apiHolder: ApiServiceHolder,
    private val downloadRepository: DownloadRepository,
    private val playbackPositionDao: PlaybackPositionDao,
    private val clientLog: ClientLogReporter
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var controllerFuture: ListenableFuture<MediaController>? = null
    private var controller: MediaController? = null
    private var progressSyncJob: Job? = null
    private var positionUpdateJob: Job? = null

    /** Synthetic ids for non-catalog queue items (DJ voice breaks, #431). */
    private var nextSyntheticTrackId: Int = -1

    private val _currentBook = MutableStateFlow<BookDetail?>(null)
    val currentBook: StateFlow<BookDetail?> = _currentBook

    private val _currentMusic = MutableStateFlow<MusicQueueState?>(null)
    val currentMusic: StateFlow<MusicQueueState?> = _currentMusic

    private val _playerKind = MutableStateFlow<PlayerKind?>(null)
    val playerKind: StateFlow<PlayerKind?> = _playerKind

    private val _currentStreamTitle = MutableStateFlow<String?>(null)
    val currentStreamTitle: StateFlow<String?> = _currentStreamTitle

    private val _isPlaying = MutableStateFlow(false)
    val isPlaying: StateFlow<Boolean> = _isPlaying

    private val _positionMs = MutableStateFlow(0L)
    val positionMs: StateFlow<Long> = _positionMs

    private val _durationMs = MutableStateFlow(0L)
    val durationMs: StateFlow<Long> = _durationMs

    private val _currentChapterIndex = MutableStateFlow(0)
    val currentChapterIndex: StateFlow<Int> = _currentChapterIndex

    // Written on the main thread only; see playerVolume().
    private val _playerVolume = MutableStateFlow(1f)

    private var currentBaseUrl: String = ""
    private var lastReportedTrackIndex: Int = -1
    // Position of the previously-playing track at the moment of a SEEK
    // discontinuity. Used to attribute "skip" with the actual played time
    // rather than the full track duration.
    private var lastDiscontinuityOldPositionMs: Long = 0L
    // Reason for the most recent discontinuity, captured before the
    // onMediaItemTransition callback fires so we can tell SEEK from AUTO.
    private var lastDiscontinuityReason: Int = -1

    private fun isMisc(): Boolean = _currentBook.value?.category == "audiobook_misc"
    private fun isMusic(): Boolean = _currentMusic.value != null

    private fun currentTrackId(): Int? =
        _currentMusic.value?.let { it.items.getOrNull(it.currentIndex)?.track?.id }

    private fun playWhenReadyReasonName(reason: Int): String = when (reason) {
        Player.PLAY_WHEN_READY_CHANGE_REASON_USER_REQUEST -> "USER_REQUEST"
        Player.PLAY_WHEN_READY_CHANGE_REASON_AUDIO_FOCUS_LOSS -> "AUDIO_FOCUS_LOSS"
        Player.PLAY_WHEN_READY_CHANGE_REASON_AUDIO_BECOMING_NOISY -> "AUDIO_BECOMING_NOISY"
        Player.PLAY_WHEN_READY_CHANGE_REASON_REMOTE -> "REMOTE"
        Player.PLAY_WHEN_READY_CHANGE_REASON_END_OF_MEDIA_ITEM -> "END_OF_MEDIA_ITEM"
        else -> "UNKNOWN($reason)"
    }

    private fun suppressionReasonName(reason: Int): String = when (reason) {
        Player.PLAYBACK_SUPPRESSION_REASON_NONE -> "NONE"
        Player.PLAYBACK_SUPPRESSION_REASON_TRANSIENT_AUDIO_FOCUS_LOSS -> "TRANSIENT_AUDIO_FOCUS_LOSS"
        else -> "UNKNOWN($reason)"
    }

    private fun playbackStateName(state: Int): String = when (state) {
        Player.STATE_IDLE -> "IDLE"
        Player.STATE_BUFFERING -> "BUFFERING"
        Player.STATE_READY -> "READY"
        Player.STATE_ENDED -> "ENDED"
        else -> "UNKNOWN($state)"
    }

    private val playerListener = object : Player.Listener {
        override fun onIsPlayingChanged(playing: Boolean) {
            _isPlaying.value = playing
        }

        /**
         * Why playback started, stopped, or never started at all (#2961).
         *
         * The 08-14 19:35Z DJ command loaded its queue, connected a controller,
         * called play() — and three seconds later sat at playing=false,
         * position 0, with no error of any kind. A denied audio-focus request
         * produces exactly that and logs nothing anywhere, because it is not a
         * failure as far as ExoPlayer is concerned. This callback is the only
         * place that distinction is visible: Media3 flips playWhenReady back to
         * false with AUDIO_FOCUS_LOSS when the request is refused.
         */
        override fun onPlayWhenReadyChanged(playWhenReady: Boolean, reason: Int) {
            val focusRelated = reason == Player.PLAY_WHEN_READY_CHANGE_REASON_AUDIO_FOCUS_LOSS
            clientLog.report(
                level = if (focusRelated) "error" else "info",
                event = "play_when_ready",
                message = "playWhenReady=$playWhenReady reason=${playWhenReadyReasonName(reason)}",
                detail = buildMap {
                    put("playWhenReady", playWhenReady.toString())
                    put("reason", playWhenReadyReasonName(reason))
                    controller?.let {
                        put("playbackState", playbackStateName(it.playbackState))
                        put("suppression", suppressionReasonName(it.playbackSuppressionReason))
                    }
                    currentTrackId()?.let { put("trackId", it.toString()) }
                },
            )
        }

        /** Playing was requested and granted, but something is holding it — most
         * often a transient focus loss to a call, alarm or assistant. */
        override fun onPlaybackSuppressionReasonChanged(reason: Int) {
            clientLog.report(
                level = if (reason == Player.PLAYBACK_SUPPRESSION_REASON_NONE) "info" else "error",
                event = "playback_suppressed",
                message = suppressionReasonName(reason),
                detail = buildMap {
                    put("suppression", suppressionReasonName(reason))
                    currentTrackId()?.let { put("trackId", it.toString()) }
                },
            )
        }

        /**
         * Nothing overrode this before (#2961), so every playback failure —
         * a 401 on the stream, a dropped Tailscale connection mid-track, a
         * malformed file — ended as silence with no trace anywhere. Ship it.
         */
        override fun onPlayerError(error: PlaybackException) {
            val item = _currentMusic.value
                ?.let { it.items.getOrNull(it.currentIndex) }
                ?.track
            clientLog.report(
                level = "error",
                event = "player_error",
                message = error.message ?: error.errorCodeName,
                detail = buildMap {
                    put("code", error.errorCodeName)
                    put("cause", error.cause?.javaClass?.simpleName ?: "none")
                    put("causeMessage", error.cause?.message ?: "")
                    item?.let {
                        put("trackId", it.id.toString())
                        put("trackTitle", it.title)
                    }
                    _currentBook.value?.let { put("bookId", it.id.toString()) }
                },
            )
        }

        override fun onPositionDiscontinuity(
            oldPosition: Player.PositionInfo,
            newPosition: Player.PositionInfo,
            @DiscontinuityReason reason: Int
        ) {
            lastDiscontinuityOldPositionMs = oldPosition.positionMs.coerceAtLeast(0)
            lastDiscontinuityReason = reason
        }

        override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
            if (!isMusic()) return
            val ctrl = controller ?: return
            val music = _currentMusic.value ?: return
            val newIndex = ctrl.currentMediaItemIndex.coerceAtLeast(0)
            // Tag the previous track. Auto-advance counts as a real listen
            // (complete). Manual next/prev counts as a skip, with the
            // played-time pulled from onPositionDiscontinuity so the server
            // can flag early-skips.
            if (lastReportedTrackIndex >= 0 && lastReportedTrackIndex != newIndex) {
                music.items.getOrNull(lastReportedTrackIndex)?.let { prev ->
                    if (reason == Player.MEDIA_ITEM_TRANSITION_REASON_AUTO) {
                        postPlayStat(prev.track.id, "complete", prev.track.durationSeconds)
                    } else {
                        val playedSeconds = lastDiscontinuityOldPositionMs / 1000.0
                        postPlayStat(prev.track.id, "skip", playedSeconds)
                    }
                }
            }
            _currentMusic.value = music.copy(currentIndex = newIndex)
            // Report start of new track
            music.items.getOrNull(newIndex)?.let { current ->
                postPlayStat(current.track.id, "start", 0.0)
            }
            lastReportedTrackIndex = newIndex
            _durationMs.value = (music.items.getOrNull(newIndex)?.track?.durationSeconds ?: 0.0)
                .let { (it * 1000).toLong() }
        }

        override fun onPlaybackStateChanged(playbackState: Int) {
            if (playbackState == Player.STATE_READY) {
                if (isMusic()) {
                    controller?.let { ctrl ->
                        _durationMs.value = ctrl.duration.coerceAtLeast(0)
                    }
                    return
                }
                // For misc books, _durationMs is set once from book.durationSeconds in play();
                // per-track STATE_READY would clobber it with current-track duration.
                if (!isMisc()) {
                    controller?.let { ctrl ->
                        _durationMs.value = ctrl.duration.coerceAtLeast(0)
                    }
                }
            }
            if (playbackState == Player.STATE_ENDED) {
                _isPlaying.value = false
                if (isMusic()) {
                    val music = _currentMusic.value ?: return
                    music.items.getOrNull(lastReportedTrackIndex)?.let { last ->
                        postPlayStat(last.track.id, "complete", last.track.durationSeconds)
                    }
                    lastReportedTrackIndex = -1
                    return
                }
                val book = _currentBook.value ?: return
                val finishedChapter = book.chapters.lastIndex.coerceAtLeast(0)
                val now = System.currentTimeMillis()
                scope.launch {
                    // Local first — record finished so resume/Continue is correct offline.
                    playbackPositionDao.upsert(
                        PlaybackPositionEntity(
                            bookId = book.id,
                            positionSeconds = book.durationSeconds,
                            chapterIndex = finishedChapter,
                            isFinished = true,
                            updatedAt = now,
                            syncedToServer = false
                        )
                    )
                    val api = apiHolder.api
                    if (api != null) {
                        try {
                            api.updateProgress(
                                book.id,
                                ProgressUpdate(
                                    positionSeconds = book.durationSeconds,
                                    chapterIndex = finishedChapter,
                                    isFinished = true
                                )
                            )
                            playbackPositionDao.markSynced(book.id, now)
                        } catch (_: Exception) { }
                    }
                }
            }
        }
    }

    private fun ensureController(onReady: (MediaController) -> Unit) {
        controller?.let { onReady(it); return }

        val token = SessionToken(context, ComponentName(context, PlaybackService::class.java))
        val future = MediaController.Builder(context, token).buildAsync()
        controllerFuture = future
        future.addListener({
            val ctrl = future.get()
            controller = ctrl
            ctrl.addListener(playerListener)
            onReady(ctrl)
            startPositionUpdates()
        }, MoreExecutors.directExecutor())
    }

    private fun buildMediaItem(uri: String, book: BookDetail, displayTitle: String): MediaItem {
        val coverUrl = if (book.hasCover) AudiplexApi.coverUrl(currentBaseUrl, book.id) else null
        return MediaItem.Builder()
            .setUri(uri)
            .setMediaMetadata(
                MediaMetadata.Builder()
                    .setTitle(displayTitle)
                    .setArtist(book.author ?: "Unknown Author")
                    .setAlbumTitle(book.series ?: book.title)
                    .setArtworkUri(coverUrl?.let { android.net.Uri.parse(it) })
                    .build()
            )
            .build()
    }

    private fun buildMusicMediaItem(item: MusicQueueItem): MediaItem {
        val uri = AudiplexApi.musicStreamUrl(currentBaseUrl, item.track.id)
        val coverUrl = if (item.albumHasCover)
            AudiplexApi.musicCoverUrl(currentBaseUrl, item.albumId)
        else null
        return MediaItem.Builder()
            .setUri(uri)
            .setMediaId("track:${item.track.id}")
            .setMediaMetadata(
                MediaMetadata.Builder()
                    .setTitle(item.track.title)
                    .setArtist(item.track.artistName ?: "Unknown Artist")
                    .setAlbumTitle(item.albumTitle)
                    .setTrackNumber(item.track.trackNumber)
                    .setArtworkUri(coverUrl?.let { android.net.Uri.parse(it) })
                    .build()
            )
            .build()
    }

    /**
     * Play an external HTTP audio stream (e.g. Radio Free Luna's /stream.mp3),
     * replacing whatever is currently playing. Play/stop/switch-source only —
     * no queue semantics apply to a stream item (endless, no duration/seek).
     */
    fun playStreamUrl(url: String, title: String) {
        finalizeMusicIfActive()
        _currentMusic.value = null
        _currentBook.value = null
        progressSyncJob?.cancel()
        _playerKind.value = PlayerKind.Stream
        _currentStreamTitle.value = title
        lastReportedTrackIndex = -1

        val item = MediaItem.Builder()
            .setUri(url)
            .setMediaId("stream:$url")
            .setMediaMetadata(
                MediaMetadata.Builder()
                    .setTitle(title)
                    .setArtist("Radio Free Luna")
                    .build()
            )
            .build()
        ensureController { ctrl ->
            ctrl.setMediaItem(item)
            ctrl.prepare()
            ctrl.play()
        }
    }

    private var localFilePath: String? = null

    fun play(book: BookDetail, baseUrl: String, startSeconds: Double = 0.0) {
        _currentMusic.value = null
        _currentStreamTitle.value = null
        lastReportedTrackIndex = -1
        _currentBook.value = book
        _playerKind.value = PlayerKind.Audiobook
        currentBaseUrl = baseUrl
        val misc = book.category == "audiobook_misc" && book.trackUrls.isNotEmpty()

        scope.launch {
            localFilePath = downloadRepository.getLocalPath(book.id)
            ensureController { ctrl ->
                val startMs = (startSeconds * 1000).toLong().coerceAtLeast(0)
                if (misc) {
                    val items = book.trackUrls.mapIndexed { idx, relUrl ->
                        val absUrl = baseUrl.trimEnd('/') + relUrl
                        val chapterTitle = book.chapters.getOrNull(idx)?.title ?: "Track ${idx + 1}"
                        buildMediaItem(absUrl, book, chapterTitle)
                    }
                    val (startChapterIdx, withinMs) = resolveMiscStart(book, startSeconds)
                    ctrl.setMediaItems(items, startChapterIdx, withinMs)
                    _durationMs.value = (book.durationSeconds * 1000).toLong()
                } else {
                    // Meditations are stored as a MediaStore content:// Uri
                    // (#839); everything else is a plain file path.
                    val uri = localFilePath?.let { path ->
                        if (MeditationStore.isContentUri(path)) path
                        else android.net.Uri.fromFile(File(path)).toString()
                    } ?: AudiplexApi.streamUrl(baseUrl, book.id)
                    ctrl.setMediaItem(buildMediaItem(uri, book, book.title), startMs)
                }
                ctrl.prepare()
                ctrl.play()
                startProgressSync()
            }
        }
    }

    private fun resolveMiscStart(book: BookDetail, startSeconds: Double): Pair<Int, Long> {
        if (startSeconds <= 0 || book.chapters.isEmpty()) return 0 to 0L
        val chapterIdx = book.chapters
            .indexOfLast { it.startSeconds <= startSeconds }
            .coerceAtLeast(0)
        val withinMs = ((startSeconds - book.chapters[chapterIdx].startSeconds) * 1000)
            .toLong()
            .coerceAtLeast(0)
        return chapterIdx to withinMs
    }

    fun playAlbum(
        album: AlbumDetail,
        baseUrl: String,
        startIndex: Int = 0,
        shuffle: Boolean = false
    ) {
        val items = album.tracks.map { track ->
            MusicQueueItem(
                track = track,
                albumId = album.id,
                albumTitle = album.title,
                albumHasCover = album.hasCover
            )
        }
        playTrackList(
            items = items,
            baseUrl = baseUrl,
            albumId = album.id,
            playlistId = null,
            title = album.title,
            startIndex = startIndex,
            shuffle = shuffle
        )
    }

    fun playPlaylist(playlist: PlaylistDetail, baseUrl: String, albumLookup: Map<Int, Pair<String, Boolean>>, startIndex: Int = 0) {
        val items = playlist.tracks.map { track ->
            val (albumTitle, hasCover) = albumLookup[track.albumId] ?: ("Unknown" to false)
            MusicQueueItem(
                track = track,
                albumId = track.albumId,
                albumTitle = albumTitle,
                albumHasCover = hasCover
            )
        }
        playTrackList(
            items = items,
            baseUrl = baseUrl,
            albumId = null,
            playlistId = playlist.id,
            title = playlist.name,
            startIndex = startIndex
        )
    }

    fun playTracks(
        tracks: List<TrackSchema>,
        baseUrl: String,
        title: String,
        albumLookup: Map<Int, Pair<String, Boolean>>,
        shuffle: Boolean = false
    ) {
        val items = tracks.map { track ->
            val (albumTitle, hasCover) = albumLookup[track.albumId] ?: ("Unknown" to false)
            MusicQueueItem(
                track = track,
                albumId = track.albumId,
                albumTitle = albumTitle,
                albumHasCover = hasCover
            )
        }
        playTrackList(
            items = items,
            baseUrl = baseUrl,
            albumId = null,
            playlistId = null,
            title = title,
            startIndex = 0,
            shuffle = shuffle
        )
    }

    private fun toDjQueueItem(track: TrackSchema): MusicQueueItem = MusicQueueItem(
        track = track,
        albumId = track.albumId,
        albumTitle = "Unknown",
        albumHasCover = false
    )

    /**
     * DJ: append tracks to the END of the current music queue. If no music
     * queue is active, starts playback of the tracks (play_now fallback).
     * Net-new Media3 op: addMediaItems.
     */
    fun enqueueTracks(tracks: List<TrackSchema>, baseUrl: String) {
        if (tracks.isEmpty()) return
        val music = _currentMusic.value
        if (music == null || _playerKind.value != PlayerKind.Music) {
            playTracks(tracks, baseUrl, title = "DJ Queue", albumLookup = emptyMap())
            return
        }
        val newItems = tracks.map { toDjQueueItem(it) }
        _currentMusic.value = music.copy(items = music.items + newItems)
        ensureController { ctrl ->
            ctrl.addMediaItems(newItems.map { buildMusicMediaItem(it) })
        }
    }

    /**
     * DJ: insert tracks immediately AFTER the currently-playing track. If no
     * music queue is active, starts playback (play_now fallback).
     * Net-new Media3 op: addMediaItems(index, ...).
     */
    fun playNextTracks(tracks: List<TrackSchema>, baseUrl: String) {
        if (tracks.isEmpty()) return
        val music = _currentMusic.value
        if (music == null || _playerKind.value != PlayerKind.Music) {
            playTracks(tracks, baseUrl, title = "DJ Queue", albumLookup = emptyMap())
            return
        }
        val insertAt = (music.currentIndex + 1).coerceIn(0, music.items.size)
        val newItems = tracks.map { toDjQueueItem(it) }
        val updated = music.items.toMutableList().apply { addAll(insertAt, newItems) }
        _currentMusic.value = music.copy(items = updated)
        ensureController { ctrl ->
            ctrl.addMediaItems(insertAt, newItems.map { buildMusicMediaItem(it) })
        }
    }

    /**
     * DJ: insert a synthesized voice-break clip into the queue (item #431).
     *
     * The clip is NOT a catalog track, so it gets a synthetic negative id and
     * its MediaItem URI is built directly from [clipUrl] — buildMusicMediaItem
     * would derive the URI from track.id, which is meaningless here. Negative
     * ids keep the break out of catalog lookups, play-stats and favorites;
     * guard `id < 0` in any new code that walks the queue.
     *
     * Auth is free: ExoPlayer streams through the authed OkHttpDataSource.
     *
     * @param playNow true = interrupt and speak immediately; false = speak
     *   after the current track finishes, which is how a real DJ break lands.
     */
    fun insertVoiceClip(
        clipUrl: String,
        clipId: Long,
        title: String,
        durationSeconds: Double?,
        playNow: Boolean
    ) {
        val clipItem = MusicQueueItem(
            track = TrackSchema(
                // Session-unique negative id. Deliberately NOT -clipId: clipId
                // is epoch-ms and overflows Int.
                id = nextSyntheticTrackId--,
                title = title,
                albumId = -1,
                artistId = -1,
                artistName = "DJ",
                discNumber = 0,
                trackNumber = 0,
                durationSeconds = durationSeconds ?: 0.0
            ),
            albumId = -1,
            albumTitle = "DJ break",
            albumHasCover = false
        )
        val mediaItem = MediaItem.Builder()
            .setUri(clipUrl)
            .setMediaId("djclip:$clipId")
            .setMediaMetadata(
                MediaMetadata.Builder()
                    .setTitle(title)
                    .setArtist("DJ")
                    .build()
            )
            .build()

        val music = _currentMusic.value
        if (music == null || _playerKind.value != PlayerKind.Music) {
            // Nothing to break between — just speak it.
            finalizeMusicIfActive()
            _currentBook.value = null
            progressSyncJob?.cancel()
            _playerKind.value = PlayerKind.Music
            lastReportedTrackIndex = -1
            _currentMusic.value = MusicQueueState(
                items = listOf(clipItem),
                albumId = null,
                playlistId = null,
                title = "DJ break",
                currentIndex = 0
            )
            ensureController { ctrl ->
                ctrl.setMediaItem(mediaItem)
                ctrl.prepare()
                ctrl.play()
            }
            return
        }

        val insertAt = (music.currentIndex + 1).coerceIn(0, music.items.size)
        val updated = music.items.toMutableList().apply { add(insertAt, clipItem) }
        // _currentMusic and the Media3 timeline must move in lockstep or
        // dj_reorder indices and now-playing reporting drift apart.
        _currentMusic.value = music.copy(items = updated)
        ensureController { ctrl ->
            ctrl.addMediaItems(insertAt, listOf(mediaItem))
            if (playNow) ctrl.seekToNextMediaItem()
        }
    }

    /**
     * DJ: move a queued track from one position to another. No-op if either
     * index is out of range or they're equal. Net-new Media3 op: moveMediaItem.
     */
    fun moveTrack(fromIndex: Int, toIndex: Int) {
        val music = _currentMusic.value ?: return
        val items = music.items
        if (fromIndex !in items.indices || toIndex !in items.indices) return
        if (fromIndex == toIndex) return
        val moving = items[fromIndex]
        val reordered = items.toMutableList().apply {
            removeAt(fromIndex)
            add(toIndex, moving)
        }
        // Keep currentIndex pointing at the same playing item after the shift.
        val current = music.currentIndex
        val newCurrent = when {
            current == fromIndex -> toIndex
            fromIndex < current && toIndex >= current -> current - 1
            fromIndex > current && toIndex <= current -> current + 1
            else -> current
        }
        _currentMusic.value = music.copy(items = reordered, currentIndex = newCurrent)
        ensureController { ctrl ->
            ctrl.moveMediaItem(fromIndex, toIndex)
        }
    }

    private fun playTrackList(
        items: List<MusicQueueItem>,
        baseUrl: String,
        albumId: Int?,
        playlistId: Int?,
        title: String,
        startIndex: Int,
        shuffle: Boolean = false
    ) {
        if (items.isEmpty()) return
        // Clear audiobook state, post final stop for any prior music
        finalizeMusicIfActive()
        _currentBook.value = null
        _currentStreamTitle.value = null
        progressSyncJob?.cancel()
        currentBaseUrl = baseUrl

        val safeIndex = startIndex.coerceIn(0, items.lastIndex)
        _currentMusic.value = MusicQueueState(
            items = items,
            albumId = albumId,
            playlistId = playlistId,
            title = title,
            currentIndex = safeIndex
        )
        _playerKind.value = PlayerKind.Music
        lastReportedTrackIndex = -1

        ensureController { ctrl ->
            val mediaItems = items.map { buildMusicMediaItem(it) }
            // Stage the start index in the timeline setup; setting shuffle
            // before play() ensures the first-track pick is shuffled too.
            ctrl.setMediaItems(mediaItems, safeIndex, 0L)
            ctrl.shuffleModeEnabled = shuffle
            ctrl.prepare()
            ctrl.play()
            // Manually fire start for the initial track since onMediaItemTransition
            // may have already fired for the same index before we updated state.
            // If it did fire, it already claimed this index — posting again gave
            // two 'start' rows milliseconds apart and skewed the taste loop
            // (#2961), so let whichever path got there first own it.
            val firstPlayingIndex = ctrl.currentMediaItemIndex.coerceAtLeast(0)
            if (lastReportedTrackIndex != firstPlayingIndex) {
                items.getOrNull(firstPlayingIndex)?.let {
                    postPlayStat(it.track.id, "start", 0.0)
                    lastReportedTrackIndex = firstPlayingIndex
                    _durationMs.value = (it.track.durationSeconds * 1000).toLong()
                }
            }
        }
    }

    private fun postPlayStat(trackId: Int, event: String, playedSeconds: Double) {
        scope.launch {
            try {
                apiHolder.api?.postPlayStat(
                    PlayStatEvent(trackId = trackId, event = event, playedSeconds = playedSeconds)
                )
            } catch (_: Exception) { }
        }
    }

    private fun finalizeMusicIfActive() {
        val music = _currentMusic.value ?: return
        music.items.getOrNull(lastReportedTrackIndex)?.let { item ->
            val playedMs = controller?.currentPosition?.coerceAtLeast(0) ?: 0L
            postPlayStat(item.track.id, "stop", playedMs / 1000.0)
        }
        lastReportedTrackIndex = -1
    }

    fun pause() {
        controller?.pause()
    }

    fun resume() {
        controller?.play()
    }

    /** Media3 player volume (0.0-1.0) — NOT device/stream volume, deliberately;
     * device volume would fight the Pantheon companion's mic-hot media-ducking. */
    fun setPlayerVolume(volume: Float) {
        val clamped = volume.coerceIn(0f, 1f)
        controller?.volume = clamped
        _playerVolume.value = clamped
    }

    /**
     * Last known player volume, safe to read from ANY thread.
     *
     * A MediaController is thread-confined to the looper it was built on, so
     * reading controller.volume off the main thread throws IllegalStateException.
     * That is precisely what killed DjCommandClient's report loop the moment
     * anything played (#2961) — silently, because the loop had no try/catch and
     * its sibling job kept running. Read the cached snapshot instead; it is
     * refreshed on the main thread by startPositionUpdates().
     */
    fun playerVolume(): Float = _playerVolume.value

    fun seekTo(positionMs: Long) {
        if (isMusic()) {
            controller?.seekTo(positionMs)
            _positionMs.value = positionMs
            return
        }
        seekToGlobalMs(positionMs)
        _positionMs.value = positionMs
    }

    fun skipForward() {
        if (isMusic()) {
            controller?.seekToNextMediaItem()
            return
        }
        val target = (_positionMs.value + 30_000).coerceAtMost(_durationMs.value)
        seekToGlobalMs(target)
        _positionMs.value = target
    }

    fun skipBack() {
        if (isMusic()) {
            controller?.seekToPreviousMediaItem()
            return
        }
        val target = (_positionMs.value - 30_000).coerceAtLeast(0)
        seekToGlobalMs(target)
        _positionMs.value = target
    }

    fun seekToChapter(index: Int) {
        val book = _currentBook.value ?: return
        val chapter = book.chapters.getOrNull(index) ?: return
        if (isMisc()) {
            controller?.seekTo(index, 0L)
        } else {
            controller?.seekTo((chapter.startSeconds * 1000).toLong())
        }
        _currentChapterIndex.value = index
    }

    fun nextChapter() {
        if (isMusic()) {
            controller?.seekToNextMediaItem()
            return
        }
        seekToChapter(_currentChapterIndex.value + 1)
    }

    fun previousChapter() {
        if (isMusic()) {
            controller?.seekToPreviousMediaItem()
            return
        }
        val book = _currentBook.value ?: return
        val currentChapter = _currentChapterIndex.value
        val currentPos = _positionMs.value / 1000.0

        val chapter = book.chapters.getOrNull(currentChapter)
        if (chapter != null && (currentPos - chapter.startSeconds) > 3.0 && currentChapter >= 0) {
            seekToChapter(currentChapter)
        } else {
            seekToChapter((currentChapter - 1).coerceAtLeast(0))
        }
    }

    fun seekToTrack(index: Int) {
        if (!isMusic()) return
        val items = _currentMusic.value?.items ?: return
        if (index !in items.indices) return
        controller?.seekTo(index, 0L)
    }

    private fun seekToGlobalMs(globalPositionMs: Long) {
        val ctrl = controller ?: return
        if (!isMisc()) {
            ctrl.seekTo(globalPositionMs)
            return
        }
        val book = _currentBook.value
        val chapters = book?.chapters
        if (book == null || chapters.isNullOrEmpty()) {
            ctrl.seekTo(globalPositionMs)
            return
        }
        val targetSec = globalPositionMs / 1000.0
        val chapterIdx = chapters.indexOfLast { it.startSeconds <= targetSec }.coerceAtLeast(0)
        val withinMs = ((targetSec - chapters[chapterIdx].startSeconds) * 1000)
            .toLong()
            .coerceAtLeast(0)
        ctrl.seekTo(chapterIdx, withinMs)
    }

    private fun currentGlobalPositionMs(): Long {
        val ctrl = controller ?: return 0L
        if (isMusic()) return ctrl.currentPosition.coerceAtLeast(0)
        if (!isMisc()) return ctrl.currentPosition.coerceAtLeast(0)
        val chapters = _currentBook.value?.chapters ?: return ctrl.currentPosition.coerceAtLeast(0)
        val idx = ctrl.currentMediaItemIndex.coerceIn(0, (chapters.size - 1).coerceAtLeast(0))
        val chapterStartMs = (chapters.getOrNull(idx)?.startSeconds ?: 0.0) * 1000
        return (chapterStartMs.toLong() + ctrl.currentPosition).coerceAtLeast(0)
    }

    private fun startPositionUpdates() {
        positionUpdateJob?.cancel()
        positionUpdateJob = scope.launch {
            while (true) {
                controller?.let { ctrl ->
                    // Main-thread refresh of the snapshot background callers read.
                    _playerVolume.value = ctrl.volume
                    if (ctrl.isPlaying) {
                        _positionMs.value = currentGlobalPositionMs()
                        if (isMusic()) {
                            updateCurrentMusicIndex()
                        } else {
                            updateCurrentChapter()
                        }
                    }
                }
                delay(250)
            }
        }
    }

    private fun updateCurrentMusicIndex() {
        val ctrl = controller ?: return
        val music = _currentMusic.value ?: return
        val idx = ctrl.currentMediaItemIndex.coerceAtLeast(0)
        if (music.currentIndex != idx) {
            _currentMusic.value = music.copy(currentIndex = idx)
        }
    }

    private fun updateCurrentChapter() {
        val book = _currentBook.value ?: return
        if (book.chapters.isEmpty()) return
        if (isMisc()) {
            controller?.let { ctrl ->
                _currentChapterIndex.value = ctrl.currentMediaItemIndex.coerceAtLeast(0)
            }
            return
        }
        val posSeconds = _positionMs.value / 1000.0
        val chapterIndex = book.chapters.indexOfLast { it.startSeconds <= posSeconds }
        if (chapterIndex >= 0) {
            _currentChapterIndex.value = chapterIndex
        }
    }

    private fun startProgressSync() {
        progressSyncJob?.cancel()
        progressSyncJob = scope.launch {
            while (true) {
                delay(30_000)
                syncProgress()
            }
        }
    }

    private suspend fun syncProgress() {
        val book = _currentBook.value ?: return
        val posSeconds = _positionMs.value / 1000.0
        // 0-guard: a stalled/not-yet-ready player reports 0 — never let that
        // clobber a good saved position with 0:00.
        if (posSeconds <= 0) return
        val chapterIndex = _currentChapterIndex.value
        val now = System.currentTimeMillis()
        // Local first — instant and offline-proof.
        playbackPositionDao.upsert(
            PlaybackPositionEntity(
                bookId = book.id,
                positionSeconds = posSeconds,
                chapterIndex = chapterIndex,
                isFinished = false,
                updatedAt = now,
                syncedToServer = false
            )
        )
        // Server best-effort; mark synced only on a successful PUT.
        val api = apiHolder.api ?: return
        try {
            api.updateProgress(
                book.id,
                ProgressUpdate(
                    positionSeconds = posSeconds,
                    chapterIndex = chapterIndex
                )
            )
            playbackPositionDao.markSynced(book.id, now)
        } catch (_: Exception) { }
    }

    fun release() {
        if (_currentBook.value != null) {
            scope.launch { syncProgress() }
        }
        if (_currentMusic.value != null) {
            finalizeMusicIfActive()
        }
        progressSyncJob?.cancel()
        positionUpdateJob?.cancel()
        controller?.removeListener(playerListener)
        controllerFuture?.let { MediaController.releaseFuture(it) }
        controller = null
    }
}
