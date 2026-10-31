package com.audiplex.app.playback

import android.content.BroadcastReceiver  // #5047
import android.content.ComponentName
import android.content.Context
import android.content.Intent  // #5047
import android.content.IntentFilter  // #5047
import android.media.AudioManager  // #5047
import androidx.core.content.ContextCompat  // #5047
import android.media.AudioDeviceInfo  // #5282
import android.media.audiofx.LoudnessEnhancer  // #4222
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.Player.DiscontinuityReason
import android.net.Uri
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.data.api.AlbumDetail
import com.audiplex.app.data.api.AudiplexApi
import com.audiplex.app.data.download.DownloadRepository
import com.audiplex.app.data.download.MeditationStore
import com.audiplex.app.data.api.BookDetail
import com.audiplex.app.data.api.MusicLevelsResponse
import com.audiplex.app.data.api.PlayStatEvent
import com.audiplex.app.data.api.PlaylistDetail
import com.audiplex.app.data.api.ProgressUpdate
import com.audiplex.app.data.api.TrackSchema
import com.audiplex.app.data.db.PlaybackPositionDao
import com.audiplex.app.data.db.PlaybackPositionEntity
import com.audiplex.app.data.db.RecentlyPlayedDao
import com.google.common.util.concurrent.ListenableFuture
import com.google.common.util.concurrent.MoreExecutors
import dagger.hilt.android.qualifiers.ApplicationContext
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.CancellationException  // #3601
import kotlinx.coroutines.flow.first  // #3601
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.isActive  // #4222
import kotlinx.coroutines.withContext  // #7109
import kotlin.math.pow  // #4222
import okhttp3.OkHttpClient
import okhttp3.Request
import javax.inject.Inject
import javax.inject.Singleton

enum class PlayerKind { Audiobook, Music, Stream }

/** Fallback metadata from the MediaController when local state is null (#3505). */
data class ControllerMetadata(
    val title: String?,
    val artist: String?
)

/** #ride0928: more than this many failures inside the window stops auto-skipping. */
internal const val SKIP_LOOP_MAX_FAILURES = 5
internal const val SKIP_LOOP_WINDOW_MS = 30_000L

/** #ride0928: only a file that is not there. A network error (tunnel blip) must NOT skip songs. */
internal val SKIPPABLE_ERROR_CODES = setOf(
    PlaybackException.ERROR_CODE_IO_BAD_HTTP_STATUS,
    PlaybackException.ERROR_CODE_IO_FILE_NOT_FOUND,
)

/**
 * #ride0928: should a player error auto-skip to the next queue item?
 *
 * Only for source/IO errors (2000..2999: bad HTTP status, file not found, ...)
 * on a real catalog MUSIC track (positive id, so not a DJ voice clip) that has
 * a next item. [recentFailures] counts errors already seen inside the window;
 * once that reaches the limit we stop skipping so a dead server cannot make
 * the queue race to its end.
 */
internal fun shouldSkipFailedTrack(
    errorCode: Int,
    kind: PlayerKind?,
    trackId: Int?,
    hasNext: Boolean,
    recentFailures: Int,
): Boolean =
    kind == PlayerKind.Music &&
        trackId != null && trackId > 0 &&
        hasNext &&
        errorCode in SKIPPABLE_ERROR_CODES &&
        recentFailures < SKIP_LOOP_MAX_FAILURES

data class MusicQueueItem(
    val track: TrackSchema,
    val albumId: Int,
    val albumTitle: String,
    val albumHasCover: Boolean
)

/** One player error, numbered so a waiter can tell a fresh one from an old one. */
data class PlayerErrorEvent(val seq: Long, val message: String)

/**
 * The queue after a DJ mix replaces its tail: items up to and including
 * [currentIndex] are untouched, then [newTail]. Pure, so the "never touch the
 * current song" invariant is unit-testable without a player.
 */
internal fun <T> replaceTail(items: List<T>, currentIndex: Int, newTail: List<T>): List<T> =
    items.take((currentIndex + 1).coerceIn(0, items.size)) + newTail

data class MusicQueueState(
    val items: List<MusicQueueItem>,
    val albumId: Int?,
    val playlistId: Int?,
    val title: String,
    val currentIndex: Int = 0,
    // #3601: who built this queue (ORIGIN_DJ / ORIGIN_MANUAL); null when
    // adopted from a session that predates the stamp. Only DJ queues come
    // back on their own after an app kill.
    val origin: String? = ORIGIN_MANUAL
)

@Singleton
class PlaybackManager @Inject constructor(
    @ApplicationContext private val context: Context,
    private val apiHolder: ApiServiceHolder,
    private val downloadRepository: DownloadRepository,
    private val playbackPositionDao: PlaybackPositionDao,
    private val recentlyPlayedDao: RecentlyPlayedDao,
    private val clientLog: ClientLogReporter,
    private val settingsStore: SettingsStore,
    private val okHttpClient: OkHttpClient
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    // #997/#3111: cached per-channel base volumes (0–1), collected from
    // SettingsStore below. Applied to controller.volume per PlayerKind at each
    // play entry (applyAudioAttributesFor) and live when a dial moves while the
    // matching kind is playing. Music+Stream share musicVolume; Audiobook has
    // its own. Written on Main by the collectors, read on Main in
    // applyAudioAttributesFor — no cross-thread hazard, so no @Volatile needed.
    private var musicVolume: Float = 1f
    private var audiobookVolume: Float = 1f
    // #4222: gain belongs to the current book and the service's actual audio session.
    private var currentBookGainDb: Double? = null
    private var bookGainJob: Job? = null
    private var bookGainPlayer: ExoPlayer? = null
    private var bookEnhancer: LoudnessEnhancer? = null
    private var bookEnhancerSessionId = C.AUDIO_SESSION_ID_UNSET
    private val bookAudioSessionListener = object : Player.Listener {
        override fun onAudioSessionIdChanged(audioSessionId: Int) {
            applyBoostEnhancer()
        }
    }
    // #4018: the sleep bed's own level (the sleep dialog's slider), independent of the book.
    private var sleepBedLevel: Float = SleepBedRules.DEFAULT_VOLUME
    // #5282: "Book at bedtime". Applies while a sleep timer with a bed is armed, during Hear the
    // mix, and afterwards while the phone is still above where bedtime started (SleepHold).
    private var sleepBookLevel: Float = SleepBedRules.DEFAULT_BOOK_LEVEL
    private var sleepArmedWithBed = false
    private var sleepFading = false  // the fade owns the main volume; see applyVolumeForCurrentKind
    private var sleepHold: SleepBedRules.SleepHold? = null
    private var mainRampJob: Job? = null
    private var previewResumedMain = false
    private var mainFadedOut = false  // the fade left the player at 0; the next play restores it, whoever presses play
    private val _mixPreview = MutableStateFlow(false)
    val mixPreview: StateFlow<Boolean> = _mixPreview

    init {
        // #4118: the service is releasing its player; drop the effect bound to its session.
        PlayerHooks.onPlayerReleasing = {
            releaseBookEnhancer()
            try { bookGainPlayer?.removeListener(bookAudioSessionListener) } catch (_: Exception) { }
            bookGainPlayer = null
        }
        scope.launch {
            settingsStore.musicVolume.collect { v ->
                musicVolume = v
                // Live-apply if a music/stream track is currently playing.
                if (_playerKind.value != PlayerKind.Audiobook) applyVolumeForCurrentKind()
            }
        }
        scope.launch {
            settingsStore.audiobookVolume.collect { v ->
                audiobookVolume = v
                if (_playerKind.value == PlayerKind.Audiobook) applyVolumeForCurrentKind()
            }
        }
        scope.launch {
            settingsStore.migrateSleepMixToAttenuate()  // #5282: before the first read
            sleepHold = settingsStore.sleepHold()
            launch {
                settingsStore.sleepBookLevel.collect { v ->
                    val changed = v != sleepBookLevel
                    sleepBookLevel = v
                    if (changed && bedtimeTrimActive()) applyVolumeForCurrentKind()
                }
            }
            settingsStore.sleepBedVolume.collect { sleepBedLevel = it }  // #4018
        }
    }

    /** #997: the base volume for [kind]. Music and Stream share the music dial. */
    private fun volumeForKind(kind: PlayerKind): Float =
        if (kind == PlayerKind.Audiobook) audiobookVolume else musicVolume

    /**
     * #7109: compute the effective music volume with per-track gain applied.
     * For non-music, returns the slider volume unchanged.
     */
    private fun effectiveMusicVolume(sliderVolume: Float): Float {
        if (!isMusic()) return sliderVolume * mainTrim()
        return (sliderVolume * currentMusicGain * mainTrim()).coerceIn(0f, 1f)
    }

    /** #5282: does the bedtime cut apply to the main player right now? */
    private fun bedtimeTrimActive(): Boolean =
        sleepArmedWithBed || _mixPreview.value || SleepBedRules.holdActive(
            sleepHold, audioManager.getStreamVolume(AudioManager.STREAM_MUSIC), currentAudioRoute(),
            System.currentTimeMillis(),
        )

    private fun mainTrim(): Float = if (bedtimeTrimActive()) SleepBedRules.trimAmplitude(sleepBookLevel) else 1f

    /** #5282: book gain as played: the server's gain, or at bedtime that plus the cut, never past 0 dB. */
    private fun bookAppliedDb(): Double {
        val gain = currentBookGainDb ?: 0.0
        return if (bedtimeTrimActive()) SleepBedRules.bookNetDb(gain, sleepBookLevel) else gain
    }

    /** #5282: names the output, since Android keeps one media volume per output. */
    private fun currentAudioRoute(): String {
        val device = audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS).firstOrNull { it.type in EXTERNAL_OUTPUTS }
        return device?.let { "${it.type}:${it.productName}" } ?: "speaker"
    }

    private fun storeSleepHold(hold: SleepBedRules.SleepHold?) {
        sleepHold = hold
        scope.launch { settingsStore.setSleepHold(hold) }
    }

    /** #5282: bedtime starts here on this output; see [SleepBedRules.SleepHold]. */
    private fun recordSleepHold() {
        storeSleepHold(
            SleepBedRules.nextHold(
                sleepHold, audioManager.getStreamVolume(AudioManager.STREAM_MUSIC), currentAudioRoute(),
                System.currentTimeMillis(),
            )
        )
    }

    private fun bedtimeRunning(): Boolean = sleepArmedWithBed || _mixPreview.value || bedWanted

    /** #5282: bedtime is over once nothing of it is left; raises overnight, with the bed on, still count. */
    private fun finishSleepHold() {
        val hold = sleepHold ?: return
        if (bedtimeRunning()) return
        storeSleepHold(
            SleepBedRules.finishHold(
                hold, audioManager.getStreamVolume(AudioManager.STREAM_MUSIC), currentAudioRoute(),
                System.currentTimeMillis(),
            )
        )
    }

    /**
     * #5282: drop the hold once he is back down or it has run its time, including a lowering that
     * happened while this process was dead. True when it dropped, so a live press can re-apply.
     */
    private fun dropSleepHoldIfDone(index: Int = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC)): Boolean {
        val hold = sleepHold ?: return false
        if (bedtimeRunning()) return false
        if (!SleepBedRules.holdCleared(hold, index, currentAudioRoute(), System.currentTimeMillis())) return false
        storeSleepHold(null)
        clientLog.report("info", "sleep_hold", "bedtime cut ended", volumeDetail())
        return true
    }

    /**
     * #7109: fetch music loudness normalization settings and levels from server.
     * #7528: at every track change, not only at queue load, so a server target
     * change applies from the next song. Reapplies the gain when they changed.
     */
    private fun fetchMusicLevels() {
        scope.launch {
            if (levelsSource.refresh()) withContext(Dispatchers.Main) { reapplyTrackGain() }
        }
    }

    /**
     * #7109: on a track change, move the player to the new track's normalized
     * level. Only while normalization is ON (off = exactly the old behaviour),
     * and never while the music looks ducked: the duck lives in the service's
     * AudioFocusManager, which this side can't see, and setting the volume
     * mid-duck would un-duck a Talk. The ducked track keeps its old gain until
     * the next change; the restore goes back to that pre-duck level.
     */
    private fun reapplyTrackGain() {
        if (musicLevels?.normalizeMusic != true) {
            if (currentMusicBoostDb != 0.0) { currentMusicBoostDb = 0.0; applyBoostEnhancer() }  // #4118
            return
        }
        val applied = _playerVolume.value
        val expected = effectiveMusicVolume(volumeForKind(PlayerKind.Music))
        updateCurrentTrackGain()
        applyBoostEnhancer()  // #4118: the raise rides on the limiter, duck or not
        // #7528: mid-duck, move the level the restore lands on instead.
        if (PlayerHooks.retargetRestore(effectiveMusicVolume(volumeForKind(PlayerKind.Music)))) return
        // #5282: a bedtime-cut ramp under way is not a duck or a fade; keep ramping, to the new track's level.
        val ramping = mainRampJob?.isActive == true
        if (!ramping && applied < expected * DUCKED_FRACTION) return  // e.g. a sleep fade: leave it be
        applyVolumeForCurrentKind(ramp = ramping)
    }

    /**
     * #7109: update the gain for the current track based on its loudness_lufs.
     * Should be called whenever the track changes.
     */
    private fun updateCurrentTrackGain() {
        val currentTrack = _currentMusic.value?.let { it.items.getOrNull(it.currentIndex)?.track }
        if (currentTrack == null || musicLevels == null || !musicLevels!!.normalizeMusic) {
            currentMusicGain = 1.0f
            currentMusicBoostDb = 0.0
            return
        }
        // #4118: the cut goes on the player volume, the raise on the limiter.
        val gainDb = LoudnessGain.gainDbFor(
            currentTrack.loudnessLufs,
            musicLevels!!.targetLufs,
            musicLevels!!.fallbackLufs,
            musicLevels!!.maxBoostDb
        )
        currentMusicGain = 10.0.pow(gainDb.coerceAtMost(0.0) / 20.0).toFloat()
        currentMusicBoostDb = gainDb.coerceAtLeast(0.0)
    }

    /**
     * #997: push the dial for the currently-active kind onto the controller.
     * Reuses setPlayerVolume so the _playerVolume snapshot (read by the DJ
     * report loop) stays coherent. No-op if nothing is playing yet — the next
     * play entry applies it via applyAudioAttributesFor.
     */
    private fun applyVolumeForCurrentKind(ramp: Boolean = false) {
        val kind = _playerKind.value ?: return
        if (sleepFading) return  // #5282: a dial or late gain write here would jump the book back up mid-fade
        mainRampJob?.cancel()
        dropSleepHoldIfDone()
        // #4222 #4118: positive gain uses the limiter; only attenuation changes player volume.
        applyBoostEnhancer()
        val target = if (kind == PlayerKind.Audiobook) {
            (audiobookVolume * 10.0.pow(bookAppliedDb().coerceAtMost(0.0) / 20.0)).toFloat()
        } else effectiveMusicVolume(volumeForKind(kind))
        if (PlayerHooks.retargetRestore(target)) return  // #7528: ducked; the restore lands on it
        if (ramp) rampMainTo(target) else setPlayerVolume(target)
    }

    /** #5282: the bedtime cut arrives and leaves over 2 s, never as a jump; a duck ends the ramp. */
    private fun rampMainTo(target: Float) {
        val volumes = SleepBedRules.rampVolumes(_playerVolume.value, target)
        mainRampJob = scope.launch {
            for (v in volumes) {
                delay(SleepBedRules.BOOK_RAMP_MS / volumes.size)
                if (PlayerHooks.retargetRestore(target)) return@launch
                setPlayerVolume(v)
            }
        }
    }

    // #4222: a failed/offline request leaves the current book's gain unchanged.
    private fun fetchBookGain(bookId: Int) {
        bookGainJob?.cancel()
        bookGainJob = scope.launch {
            try {
                val gain = apiHolder.api?.getGain("book", bookId) ?: return@launch
                if (!isActive || _playerKind.value != PlayerKind.Audiobook ||
                    _currentBook.value?.id != bookId) return@launch
                if (gain.gainDb != null && !gain.gainDb.isFinite()) return@launch
                currentBookGainDb = gain.gainDb
                applyVolumeForCurrentKind()
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) { }
        }
    }

    // #4222: MediaController has no audio session id; use the existing service player hook.
    @OptIn(UnstableApi::class)
    private fun applyBoostEnhancer() {
        try {
            val player = PlayerHooks.exoPlayer
            if (bookGainPlayer !== player) {
                bookGainPlayer?.removeListener(bookAudioSessionListener)
                releaseBookEnhancer()
                bookGainPlayer = player
                player?.addListener(bookAudioSessionListener)
            }
            val gainDb = when {  // #4118: music raises share the book's limiter
                _playerKind.value == PlayerKind.Audiobook -> bookAppliedDb()  // #5282
                _playerKind.value == PlayerKind.Music && isMusic() && !bedtimeTrimActive() -> currentMusicBoostDb
                else -> 0.0
            }
            val sessionId = player?.audioSessionId ?: C.AUDIO_SESSION_ID_UNSET
            if (gainDb <= 0.0 ||
                sessionId == C.AUDIO_SESSION_ID_UNSET) {
                releaseBookEnhancer()
                return
            }
            if (bookEnhancer == null || bookEnhancerSessionId != sessionId) {
                releaseBookEnhancer()
                bookEnhancer = LoudnessEnhancer(sessionId)
                bookEnhancerSessionId = sessionId
            }
            // #4222: LoudnessEnhancer's built-in limiter handles boosts above unity.
            // #4118: hard ceiling here too, whatever the server sends (never blare).
            bookEnhancer?.setTargetGain((gainDb.coerceAtMost(MAX_BOOST_DB) * 100).toInt())  // millibels
            bookEnhancer?.enabled = true
        } catch (_: Exception) {
            releaseBookEnhancer()
        }
    }

    // #4222: devices can throw even while disabling or releasing an audio effect.
    private fun releaseBookEnhancer() {
        val enhancer = bookEnhancer
        bookEnhancer = null
        bookEnhancerSessionId = C.AUDIO_SESSION_ID_UNSET
        try { enhancer?.enabled = false } catch (_: Exception) { }
        try { enhancer?.release() } catch (_: Exception) { }
    }

    // #4222: cancel stale responses and detach from the old player when leaving a book.
    private fun resetBookGain() {
        bookGainJob?.cancel()
        bookGainJob = null
        currentBookGainDb = null
        releaseBookEnhancer()
        try { bookGainPlayer?.removeListener(bookAudioSessionListener) } catch (_: Exception) { }
        bookGainPlayer = null
    }

    /**
     * #7528: the DJ "volume" command sets the playing channel's saved dial, so
     * it sticks across tracks and per-track gain, and a duck restores to it.
     */
    fun setChannelVolume(volume: Float) {
        val clamped = volume.coerceIn(0f, 1f)
        scope.launch {
            if (channelForVolumeCommand(_playerKind.value) == PlayerKind.Audiobook) {
                settingsStore.setAudiobookVolume(clamped)
            } else {
                settingsStore.setMusicVolume(clamped)
            }
        }
    }
    private var controllerFuture: ListenableFuture<MediaController>? = null
    private var controller: MediaController? = null
    private var progressSyncJob: Job? = null
    private var positionUpdateJob: Job? = null

    // Sleep engine (#1728): a second, independent player for a continuously
    // looping ambient "bed" plus a fade-out timer on the main controller.
    // Deliberately NOT routed through MediaController/PlaybackService's
    // MediaSession — it has no lock-screen UI, so it cannot touch the
    // existing single-stream session (Audiobook/Music/Stream) at all.
    private var bedPlayer: ExoPlayer? = null
    // #4018: the bed follows its real player, so a bed that died overnight
    // reads Stopped (with Restart) instead of "playing" forever.
    private val _bedState = MutableStateFlow(BedState.Off)
    val bedState: StateFlow<BedState> = _bedState
    private var bedWanted = false
    private var bedUrl: String? = null          // the bed asked for
    private var bedFallbackUrl: String? = null  // the brown-noise bed, played from its local copy
    private var bedOnFallback = false
    private var bedFailures = 0
    private var bedStartedAtMs = 0L
    private var bedRecovering = false
    private var bedRetryJob: Job? = null
    private var bedCurrentVolume = 0f           // player volume the bed plays at right now
    private var bedTargetLevel = 0f             // #5131: survives a restart
    private val bedDownloads = mutableSetOf<String>()
    private var sleepTimerJob: Job? = null
    // #3714: when the armed sleep fade STARTS (wall clock), null when none is
    // armed or it has finished; the app's sleep button reads it, whoever armed it.
    private val _sleepEndsAtMs = MutableStateFlow<Long?>(null)
    val sleepEndsAtMs: StateFlow<Long?> = _sleepEndsAtMs
    private var sleepFadeSeconds = SleepFade.DEFAULT_FADE_SECONDS
    private var sleepBedFadeTo: Float? = null

    // #5047: media-volume presses (side buttons or another app) while Audiplex is
    // playing or sleep mode is armed, so a by-hand fix like 10/25's is measurable.
    // A burst of presses within VOLUME_BURST_MS is one entry, first before to last after.
    private val audioManager = context.getSystemService(AudioManager::class.java)
    private var volumeBurstJob: Job? = null
    private var volumeBurstFrom = -1
    private val volumeReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, intent: Intent) {
            if (intent.getIntExtra(EXTRA_VOLUME_STREAM_TYPE, -1) != AudioManager.STREAM_MUSIC) return
            val before = intent.getIntExtra(EXTRA_PREV_VOLUME_STREAM_VALUE, -1)
            val after = intent.getIntExtra(EXTRA_VOLUME_STREAM_VALUE, -1)
            if (before != after && dropSleepHoldIfDone(after)) applyVolumeForCurrentKind(ramp = true)  // #5282
            if (before == after || !(isPlaying.value || bedWanted || _sleepEndsAtMs.value != null)) return
            if (volumeBurstFrom < 0) volumeBurstFrom = before
            volumeBurstJob?.cancel()
            volumeBurstJob = scope.launch {
                delay(VOLUME_BURST_MS)
                val from = volumeBurstFrom
                volumeBurstFrom = -1
                if (from != after) {
                    clientLog.report(
                        "info", "stream_volume", "media volume $from -> $after",
                        volumeDetail() + mapOf("before" to "$from", "after" to "$after"),
                    )
                }
            }
        }
    }

    init {
        ContextCompat.registerReceiver(
            context, volumeReceiver, IntentFilter(VOLUME_CHANGED_ACTION), ContextCompat.RECEIVER_NOT_EXPORTED,
        )
    }

    /** #5047: what was playing, and how loud, alongside a volume press or the sleep handoff. */
    private fun volumeDetail(): Map<String, String> = mapOf(
        "stream" to "${audioManager.getStreamVolume(AudioManager.STREAM_MUSIC)}",
        "max" to "${audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC)}",
        "playing" to "${isPlaying.value}",
        "kind" to "${_playerKind.value}",
        "book_gain_db" to "${currentBookGainDb ?: ""}",
        "bed" to "${_bedState.value}",
        "bed_url" to (bedUrl ?: ""),
        "bed_level" to "$bedCurrentVolume",
        "book_bedtime_level" to "$sleepBookLevel",  // #5282, and the bed has no boost stage left
        "book_net_db" to "${bookAppliedDb()}",
        "bedtime_cut" to "${bedtimeTrimActive()}",
        "sleep_hold_index" to "${sleepHold?.index ?: ""}",
        "sleep_armed" to "${_sleepEndsAtMs.value != null}",
    )

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

    /**
     * The most recent player error, numbered so a waiter can tell a NEW one
     * from a stale one. Lets the DJ ack say "failed: <why>" instead of "ok"
     * when a play_now never actually started (#2843).
     */
    private val _lastPlayerError = MutableStateFlow<PlayerErrorEvent?>(null)
    val lastPlayerError: StateFlow<PlayerErrorEvent?> = _lastPlayerError

    private val _positionMs = MutableStateFlow(0L)
    val positionMs: StateFlow<Long> = _positionMs

    private val _durationMs = MutableStateFlow(0L)
    val durationMs: StateFlow<Long> = _durationMs

    private val _currentChapterIndex = MutableStateFlow(0)
    val currentChapterIndex: StateFlow<Int> = _currentChapterIndex

    // Written on the main thread only; see playerVolume().
    private val _playerVolume = MutableStateFlow(1f)

    /** Controller's current metadata when local state is null (#3505). */
    private val _controllerMetadata = MutableStateFlow<ControllerMetadata?>(null)
    val controllerMetadata: StateFlow<ControllerMetadata?> = _controllerMetadata

    // #7109: per-track music loudness normalization settings and current track gain
    private val levelsSource = MusicLevelsSource { apiHolder.api?.getMusicLevels() }
    private val musicLevels: MusicLevelsResponse? get() = levelsSource.current
    // #4222: the server also serves gain_db on TrackSchema; keep the #3504 step 3 music math.
    private var currentMusicGain: Float = 1.0f
    private var currentMusicBoostDb: Double = 0.0  // #4118: applied by the LoudnessEnhancer
    /** #4118: the most any boost may raise playback, books or music (Jarvis option B, 10/18). */
    private val MAX_BOOST_DB = 6.0
    /** #7109: below this share of the expected level the player is ducked (ducks go to ~5%). */
    private val DUCKED_FRACTION = 0.5f
    private val EXTERNAL_OUTPUTS = setOf(  // #5282
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP, AudioDeviceInfo.TYPE_BLE_HEADSET, AudioDeviceInfo.TYPE_USB_HEADSET,
        AudioDeviceInfo.TYPE_WIRED_HEADSET, AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
    )

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
    /** #ride0928: timestamps of recent player errors, for the skip-loop guard. */
    private val recentPlayerErrorsMs = ArrayDeque<Long>()

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
            if (playing && mainFadedOut) {  // #5282: lock screen, notification or a headset button too
                mainFadedOut = false
                applyVolumeForCurrentKind()
            }
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
            if (!playWhenReady) reportPauseSource(reason)  // #3552
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
            _lastPlayerError.value = PlayerErrorEvent(
                seq = (_lastPlayerError.value?.seq ?: 0L) + 1,
                message = error.message ?: error.errorCodeName,
            )
            val item = _currentMusic.value
                ?.let { it.items.getOrNull(it.currentIndex) }
                ?.track
            // #ride0928: a music file that cannot be read should not strand the
            // queue in silence: skip to the next item, unless failures pile up.
            val now = System.currentTimeMillis()
            recentPlayerErrorsMs.removeAll { now - it > SKIP_LOOP_WINDOW_MS }
            val ctrl = controller
            val skipped = shouldSkipFailedTrack(
                errorCode = error.errorCode,
                kind = _playerKind.value,
                trackId = item?.id,
                hasNext = ctrl?.hasNextMediaItem() == true,
                recentFailures = recentPlayerErrorsMs.size,
            )
            recentPlayerErrorsMs.add(now)
            if (skipped && ctrl != null) {
                ctrl.seekToNextMediaItem()
                ctrl.prepare()
                ctrl.play()
            }
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
                    put("skipped", skipped.toString()) // #ride0928
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

        /**
         * Update controller metadata for fallback mini-player when local state is null (#3505).
         * Tracks the currently-playing track's title/artist from MediaMetadata.
         */
        override fun onMediaMetadataChanged(mediaMetadata: MediaMetadata) {
            updateControllerMetadata(mediaMetadata)
        }

        /**
         * Also update metadata when a new media item transitions.
         */
        override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
            if (mediaItem != null) {
                updateControllerMetadata(mediaItem.mediaMetadata)
            }
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
            reapplyTrackGain()  // #7109
            fetchMusicLevels()  // #7528: a changed server target applies from this song
            // Report start of new track
            music.items.getOrNull(newIndex)?.let { current ->
                postPlayStat(current.track.id, "start", 0.0)
                recordRecentlyPlayed(current)
            }
            lastReportedTrackIndex = newIndex
            _durationMs.value = (music.items.getOrNull(newIndex)?.track?.durationSeconds ?: 0.0)
                .let { (it * 1000).toLong() }
        }
    }

    /**
     * Update the fallback controller metadata when local state is null (#3505).
     * Used by the Player.Listener when MediaController metadata changes, so the
     * mini-player can show title/artist even if UI hydration hasn't completed.
     */
    private fun updateControllerMetadata(metadata: MediaMetadata) {
        // Always tracked; the UI decides (NowPlayingVisibility) whether local
        // state covers it. Gating here left it stale/null exactly when the UI
        // lost its local state after the fact.
        val title = metadata.title?.toString()
        val artist = metadata.artist?.toString()
        _controllerMetadata.value =
            if (title == null && artist == null) null else ControllerMetadata(title, artist)
    }

    /**
     * Re-read the live session on Activity resume (#3505). [connect] is a
     * no-op once a controller exists, so a walk-up to a DJ session that began
     * after the controller connected never re-hydrated: the mini-player stayed
     * blank while the music played. This re-runs hydration (still guarded to
     * never clobber local state) and re-seeds the fallback metadata and
     * play/pause flag from the controller.
     */
    fun refreshFromController() {
        ensureController { ctrl ->
            // #3601: a session released while we were away (app swiped while
            // paused) leaves our music state pointing at nothing. Drop it so the
            // UI isn't a dead row, then let the restore below bring it back.
            if (ctrl.mediaItemCount == 0 && _playerKind.value == PlayerKind.Music && !ctrl.isPlaying) {
                _currentMusic.value = null
                _playerKind.value = null
            }
            hydrateFromController(ctrl)
            restoreLastQueueIfIdle(ctrl)  // #3601
            if (ctrl.mediaItemCount == 0) {
                _controllerMetadata.value = null
            } else {
                updateControllerMetadata(ctrl.mediaMetadata)
            }
            _isPlaying.value = ctrl.isPlaying
        }
    }

    /**
     * #3099/#991: set the player's audio content type per PlayerKind on the
     * controller. MUSIC (music/DJ/stream) lets agent speech DUCK playback;
     * SPEECH (audiobooks) keeps the narrator PAUSING under other audio. The
     * service-side FocusPolicy reads this content type on every focus change
     * and turns a can-duck loss into a full pause only for SPEECH (it replaced
     * Media3's willPauseWhenDucked, which did the same but ducked to ~20%).
     * handleAudioFocus stays false here: focus is owned by the service's
     * AudioFocusManager, not by the controller. Called at each play entry so
     * switching kinds re-applies the right type (the PlaybackService default
     * is MUSIC, so the priority DJ-duck case is correct even before this fires).
     */
    private fun applyAudioAttributesFor(ctrl: MediaController, kind: PlayerKind) {
        val contentType =
            if (kind == PlayerKind.Audiobook) C.AUDIO_CONTENT_TYPE_SPEECH
            else C.AUDIO_CONTENT_TYPE_MUSIC
        ctrl.setAudioAttributes(
            AudioAttributes.Builder()
                .setUsage(C.USAGE_MEDIA)
                .setContentType(contentType)
                .build(),
            /* handleAudioFocus = */ false  // Audio focus handled by PlaybackService
        )
        // #997/#3111: set this kind's base volume as playback (re)starts. This
        // is the steady-state level; the service's AudioFocusManager ducks to
        // ~5% during a transient focus loss and restores to whatever the dial
        // was (it records the pre-duck volume, never a hardcoded 1.0).
        // Via setPlayerVolume so the DJ-report volume snapshot stays coherent.
        // #4222: apply book gain as its audio attributes become active.
        if (kind == PlayerKind.Audiobook) applyVolumeForCurrentKind()
        else setPlayerVolume(volumeForKind(kind) * mainTrim())  // #5282
    }

    private fun ensureController(onReady: (MediaController) -> Unit) {
        controller?.let { onReady(it); return }

        val token = SessionToken(context, ComponentName(context, PlaybackService::class.java))
        val future = MediaController.Builder(context, token)
            .setListener(object : MediaController.Listener {
                // #3601: the session was released (service stopped). Forget the
                // dead controller so the next call builds a live one and
                // re-hydrates, instead of sending play/pause into nothing.
                override fun onDisconnected(controller: MediaController) {
                    if (this@PlaybackManager.controller === controller) {
                        this@PlaybackManager.controller = null
                        controllerFuture = null
                    }
                }
            })
            .buildAsync()
        controllerFuture = future
        future.addListener({
            val ctrl = future.get()
            controller = ctrl
            ctrl.addListener(playerListener)
            // A controller connecting to an ALREADY-PLAYING session (the app
            // opened after a screen-off DJ session, or after a process restart)
            // must adopt the session's live state — otherwise the now-playing
            // UI stays blank while the MediaSession, its notification and the
            // media button all keep working, which is exactly the "no track /
            // no pause control in the app" gap Todd hit (#3105/#993). Runs
            // before onReady so a fresh play command still wins if there is one.
            hydrateFromController(ctrl)
            onReady(ctrl)
            startPositionUpdates()
        }, MoreExecutors.directExecutor())
    }

    /**
     * Connect the MediaController to whatever the session is doing, without
     * issuing a command. Called when the UI first appears (#3105/#993) so a
     * walk-up to an in-progress DJ session hydrates the now-playing surfaces
     * immediately. Idempotent — reuses the existing controller if one is up.
     */
    fun connect() {
        ensureController { }
    }

    /**
     * Adopt the session's current MUSIC queue into the UI state flows when we
     * have none of our own (#3105/#993).
     *
     * Guarded to never clobber live local state: it only fills in when nothing
     * is currently loaded. Only music timelines are reconstructable — music
     * items carry a `track:<id>` mediaId plus identity extras (see
     * buildMusicMediaItem); books/streams have neither, so their items resolve
     * to nothing and we leave those kinds alone.
     */
    private fun hydrateFromController(ctrl: MediaController) {
        if (_playerKind.value != null || _currentMusic.value != null || _currentBook.value != null) return
        val count = ctrl.mediaItemCount
        if (count == 0) return
        // #3601: DJ clips stay in as placeholders. Dropping them shifted every
        // later index, so the wrong song showed as current after a clip.
        var origin: String? = null
        val items = (0 until count).map { i ->
            val mi = ctrl.getMediaItemAt(i)
            val md = mi.mediaMetadata
            val trackId = mi.mediaId.removePrefix("track:").toIntOrNull()
                ?: return@map placeholderItem(md.title?.toString() ?: "DJ break")
            if (origin == null) origin = md.extras?.getString(EXTRA_QUEUE_ORIGIN)
            val extras = md.extras
            val albumId = extras?.getInt("albumId") ?: 0
            MusicQueueItem(
                track = TrackSchema(
                    id = trackId,
                    title = md.title?.toString() ?: "",
                    albumId = albumId,
                    artistId = extras?.getInt("artistId") ?: 0,
                    artistName = md.artist?.toString(),
                    discNumber = extras?.getInt("discNumber") ?: 0,
                    trackNumber = md.trackNumber ?: 0,
                    durationSeconds = extras?.getDouble("durationSeconds") ?: 0.0,
                ),
                albumId = albumId,
                albumTitle = extras?.getString("albumTitle") ?: md.albumTitle?.toString() ?: "Unknown",
                albumHasCover = extras?.getBoolean("albumHasCover") ?: false,
            )
        }
        if (items.none { it.track.id > 0 }) return  // not a music timeline — leave it be (#3601)
        val idx = ctrl.currentMediaItemIndex.coerceIn(0, items.lastIndex)
        _currentMusic.value = MusicQueueState(
            items = items,
            albumId = null,
            playlistId = null,
            title = "Now playing",
            currentIndex = idx,
            origin = origin,  // #3601
        )
        _playerKind.value = PlayerKind.Music
        _isPlaying.value = ctrl.isPlaying
        _durationMs.value = ctrl.duration.coerceAtLeast(0)
        lastReportedTrackIndex = idx
        clientLog.report(
            level = "info",
            event = "hydrate_from_controller",
            message = "rehydrated ${items.size} music items at index $idx",
            detail = buildMap {
                put("count", items.size.toString())
                put("index", idx.toString())
                put("isPlaying", ctrl.isPlaying.toString())
            },
        )
    }

    /** A queue row for a DJ clip/stream item, so indices match the session (#3601). */
    private fun placeholderItem(title: String) = MusicQueueItem(
        track = TrackSchema(
            id = nextSyntheticTrackId--, title = title, albumId = -1, artistId = -1,
            artistName = "DJ", discNumber = 0, trackNumber = 0, durationSeconds = 0.0,
        ),
        albumId = -1,
        albumTitle = "DJ break",
        albumHasCover = false,
    )

    // #3601/#7528: one attempt at a time. Retried on every foreground while
    // nothing is loaded; it used to be once per process, so a miss stayed missed.
    private var restoreInFlight = false

    /**
     * Put the last DJ-built queue back, PAUSED, when the app comes up with
     * nothing loaded and no live session (#3601).
     *
     * The queue lives only in this process's memory: pause, then the app gets
     * closed, and a 1,300-track DJ mix was gone with no way to rebuild it. The
     * server keeps the last queue the phone reported; this reloads it at the
     * same song and spot without making a sound. Todd's own queues come back
     * only if he turned that on in Settings, so by default they behave as before.
     */
    private fun restoreLastQueueIfIdle(ctrl: MediaController) {
        if (restoreInFlight) return
        if (_playerKind.value != null || _currentMusic.value != null || _currentBook.value != null) return
        if (ctrl.mediaItemCount > 0) return
        restoreInFlight = true
        scope.launch {
            try {
                val api = apiHolder.api ?: return@launch
                val snap = api.getResume()
                val allowManual = settingsStore.restoreManualQueues.first()
                val ids = idsToRestore(snap, allowManual)
                if (ids.isEmpty()) return@launch
                val head = resolveConcurrently(ids.take(RESTORE_HEAD)) { api.getTrack(it) }
                if (head.isEmpty()) return@launch
                // Something started while we were fetching: it wins.
                if (_playerKind.value != null || (controller?.mediaItemCount ?: 0) > 0) return@launch
                val origin = snap.origin ?: ORIGIN_DJ
                val startMs = if (head.first().id == ids.first()) snap.positionMs else 0L
                playTracks(
                    tracks = head,
                    baseUrl = apiHolder.baseUrl,
                    title = if (origin == ORIGIN_DJ) "DJ Queue" else "Now playing",
                    albumLookup = emptyMap(),
                    startPositionMs = startMs,
                    origin = origin,
                    play = false,
                )
                clientLog.report(
                    level = "info",
                    event = "queue_restored",
                    message = "restored ${ids.size} track(s) paused at ${head.first().id}",
                    detail = mapOf("origin" to origin, "position_ms" to startMs.toString()),
                )
                val rest = ids.drop(RESTORE_HEAD)
                if (rest.isNotEmpty()) {
                    val tail = resolveConcurrently(rest) { api.getTrack(it) }
                    // Only if nothing replaced the restored queue meanwhile.
                    if (_currentMusic.value?.title == (if (origin == ORIGIN_DJ) "DJ Queue" else "Now playing")) {
                        enqueueTracks(tail, apiHolder.baseUrl)
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                // 404 = nothing saved; anything else just means no restore.
            } finally {
                restoreInFlight = false
            }
        }
    }

    private fun buildMediaItem(uri: String, book: BookDetail, displayTitle: String): MediaItem {
        val coverUrl = if (book.hasCover) AudiplexApi.coverUrl(currentBaseUrl, book.id) else null
        return MediaItem.Builder()
            .setUri(uri)
            // #4217: the stream cache keys on this; the size changes when a book's file
            // is replaced, so a rescanned book never reuses a stale index.
            .setCustomCacheKey("$uri@${book.fileSize}")
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

    private fun buildMusicMediaItem(item: MusicQueueItem, origin: String? = null): MediaItem {
        val uri = AudiplexApi.musicStreamUrl(currentBaseUrl, item.track.id)
        val coverUrl = if (item.albumHasCover)
            AudiplexApi.musicCoverUrl(currentBaseUrl, item.albumId)
        else null
        // Stash the identity fields the UI needs but MediaMetadata has no slot
        // for. The MediaSession replicates these extras to any controller that
        // connects later, so hydrateFromController() can rebuild a FAITHFUL
        // queue (correct albumId → artwork, duration) after a process/Activity
        // restart — not a lossy title-only placeholder (#3105/#993).
        val extras = android.os.Bundle().apply {
            if (origin != null) putString(EXTRA_QUEUE_ORIGIN, origin)  // #3601
            putInt("albumId", item.albumId)
            putBoolean("albumHasCover", item.albumHasCover)
            putString("albumTitle", item.albumTitle)
            putInt("artistId", item.track.artistId)
            putInt("discNumber", item.track.discNumber)
            putDouble("durationSeconds", item.track.durationSeconds)
        }
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
                    .setExtras(extras)
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
        resetBookGain()  // #4222
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
            applyAudioAttributesFor(ctrl, PlayerKind.Stream) // #3099/#991: stream ducks like music
            ctrl.setMediaItem(item)
            ctrl.prepare()
            ctrl.play()
        }
    }

    private var localFilePath: String? = null

    fun play(book: BookDetail, baseUrl: String, startSeconds: Double = 0.0) {
        if (_currentBook.value?.id != book.id) resetBookGain()  // #4222
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
                applyAudioAttributesFor(ctrl, PlayerKind.Audiobook) // #3099/#991: narrator pauses, not ducks
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
                fetchBookGain(book.id)  // #4222
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
        shuffle: Boolean = false,
        // Transfer handoff (#2021): resume the first track mid-way.
        startPositionMs: Long = 0L,
        // #3601: only the DJ calls this; play=false loads the queue paused.
        origin: String = ORIGIN_DJ,
        play: Boolean = true
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
            shuffle = shuffle,
            startPositionMs = startPositionMs,
            origin = origin,  // #3601
            play = play
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
     * DJ mix (#2842): keep the current item exactly as it is — still playing,
     * same position — and replace everything AFTER it with [tracks]. An empty
     * list just trims the tail. Nothing loaded → starts [tracks]; an audiobook
     * or stream is never replaced.
     */
    fun replaceUpcoming(tracks: List<TrackSchema>, baseUrl: String) {
        val music = _currentMusic.value
        if (_playerKind.value != null && _playerKind.value != PlayerKind.Music) return
        if (music == null) {
            if (tracks.isNotEmpty()) {
                playTracks(tracks, baseUrl, title = "DJ Queue", albumLookup = emptyMap())
            }
            return
        }
        val keep = (music.currentIndex + 1).coerceIn(0, music.items.size)
        val newItems = tracks.map { toDjQueueItem(it) }
        val oldSize = music.items.size
        _currentMusic.value = music.copy(items = replaceTail(music.items, music.currentIndex, newItems))
        ensureController { ctrl ->
            if (keep < oldSize) ctrl.removeMediaItems(keep, oldSize)
            if (newItems.isNotEmpty()) ctrl.addMediaItems(newItems.map { buildMusicMediaItem(it) })
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
            resetBookGain()  // #4222
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
        shuffle: Boolean = false,
        startPositionMs: Long = 0L,
        origin: String = ORIGIN_MANUAL,  // #3601
        play: Boolean = true  // #3601: false = load paused (restore after a kill)
    ) {
        if (items.isEmpty()) return
        PlayerHooks.cancelStopAfterCurrent()  // #3505: a new queue is a fresh start
        resetBookGain()  // #4222
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
            currentIndex = safeIndex,
            origin = origin  // #3601
        )
        _playerKind.value = PlayerKind.Music
        // #7109: fetch music loudness normalization settings
        fetchMusicLevels()
        lastReportedTrackIndex = -1

        ensureController { ctrl ->
            applyAudioAttributesFor(ctrl, PlayerKind.Music) // #3099/#991: agent speech ducks music
            reapplyTrackGain()  // #7109: the first track (again once the levels arrive)
            val mediaItems = items.map { buildMusicMediaItem(it, origin) }  // #3601
            // Stage the start index in the timeline setup; setting shuffle
            // before play() ensures the first-track pick is shuffled too.
            ctrl.setMediaItems(mediaItems, safeIndex, startPositionMs)
            ctrl.shuffleModeEnabled = shuffle
            ctrl.prepare()
            if (!play) {  // #3601: restored paused; nothing started, so no start stat
                _isPlaying.value = false
                return@ensureController
            }
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
                    recordRecentlyPlayed(it)
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

    /**
     * Remember a track the moment it starts, for the Recently-played list
     * (#3107/#993). Local-only and best-effort — a DB hiccup must never disturb
     * playback. Synthetic DJ voice-break clips (negative track id) are filtered
     * out by RecentlyPlayed.fromQueueItem.
     */
    private fun recordRecentlyPlayed(item: MusicQueueItem) {
        val row = RecentlyPlayed.fromQueueItem(item, System.currentTimeMillis()) ?: return
        scope.launch {
            try {
                recentlyPlayedDao.record(row)
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

    /**
     * Start (or replace) the looping sleep-bed layer (#1728). A fresh, private
     * ExoPlayer — not the shared MediaController — so it can run alongside
     * whatever the main player is doing with its own volume, and cannot
     * regress the existing single-stream playback path. Loops forever via
     * REPEAT_MODE_ONE until [bedStop] is called; the auth'd OkHttp client
     * matches PlaybackService so a catalog stream URL (not just an external
     * one) works here too.
     *
     * #4018: it plays a LOCAL copy once one is downloaded (no network needed
     * overnight), holds a wake + Wi-Fi lock, retries errors at 10/30/60 s and
     * then falls back to the local brown-noise bed. While it plays,
     * PlaybackService keeps the process foreground (PlayerHooks.bedState).
     */
    fun bedPlay(url: String, volume: Float) {
        bedRetryJob?.cancel()
        bedWanted = true
        bedUrl = url
        bedOnFallback = false
        bedFailures = 0
        startBedPlayer(url, volume)
        cacheBed(url)
    }

    @OptIn(UnstableApi::class)
    private fun startBedPlayer(url: String, volume: Float) {
        disposeBedPlayer()
        val local = bedCacheFile(url).takeIf { it.exists() }
        // DefaultDataSource reads the file:// local copy and hands http(s) to the auth'd client.
        val dataSourceFactory = DefaultDataSource.Factory(context, OkHttpDataSource.Factory(okHttpClient))
        val player = ExoPlayer.Builder(context)
            .setMediaSourceFactory(DefaultMediaSourceFactory(dataSourceFactory))
            .setWakeMode(C.WAKE_MODE_NETWORK)  // #4018: CPU + Wi-Fi stay up with the screen off
            .build()
        bedPlayer = player
        player.setAudioAttributes(
            AudioAttributes.Builder()
                .setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
                .build(),
            // Secondary player: must never compete with the main session for
            // focus. Trade-off: the bed no longer ducks under agent TTS (Media3
            // used to); it is ambient at low volume, revisit if it bleeds into
            // the mic.
            /* handleAudioFocus = */ false
        )
        player.addListener(object : Player.Listener {
            override fun onPlaybackStateChanged(playbackState: Int) {
                if (bedPlayer === player) refreshBedState()
            }
            override fun onPlayWhenReadyChanged(playWhenReady: Boolean, reason: Int) {
                if (bedPlayer === player) refreshBedState()
            }
            override fun onPlayerError(error: PlaybackException) {
                if (bedPlayer === player) onBedError(url, local != null, error)
            }
        })
        player.repeatMode = Player.REPEAT_MODE_ONE
        bedTargetLevel = volume.coerceIn(0f, 1f)
        applyBedLevel(bedTargetLevel)
        player.setMediaItem(MediaItem.fromUri(local?.let { Uri.fromFile(it) } ?: Uri.parse(url)))
        player.prepare()
        player.play()
        bedStartedAtMs = System.currentTimeMillis()
        bedRecovering = false
        refreshBedState()
    }

    /** #4018: retry, fall back to the local brown-noise bed, or give up; each step is logged. */
    private fun onBedError(url: String, wasLocal: Boolean, error: PlaybackException) {
        if (System.currentTimeMillis() - bedStartedAtMs >= SleepBedRules.HEALTHY_RESET_MS) bedFailures = 0
        bedFailures++
        val fallback = bedFallbackUrl?.takeIf {
            SleepBedRules.cacheFileName(it) != SleepBedRules.cacheFileName(url) && bedCacheFile(it).exists()
        }
        val action = SleepBedRules.recoveryFor(bedFailures, bedOnFallback, fallback != null)
        disposeBedPlayer()
        val detail = mapOf(
            "url" to url, "local" to wasLocal.toString(), "failures" to bedFailures.toString(),
            "error" to error.errorCodeName, "action" to action.toString(),
        )
        when (action) {
            is SleepBedRules.Recovery.Retry -> {
                clientLog.report("error", "bed_error", "sleep bed failed; retrying in ${action.delayMs / 1000}s", detail)
                bedRecovering = true
                bedRetryJob = scope.launch {
                    delay(action.delayMs)
                    if (bedWanted) startBedPlayer(url, bedTargetLevel)
                }
            }
            SleepBedRules.Recovery.Fallback -> {
                clientLog.report("error", "bed_fallback", "sleep bed failed; playing the local brown-noise bed", detail)
                bedOnFallback = true
                bedFailures = 0
                startBedPlayer(fallback!!, bedTargetLevel)
                return
            }
            SleepBedRules.Recovery.GiveUp -> {
                clientLog.report("error", "bed_stopped", "sleep bed gave up after retries", detail)
                bedRecovering = false
            }
        }
        refreshBedState()
    }

    private fun refreshBedState() {
        val player = bedPlayer
        val state = SleepBedRules.stateFor(
            bedWanted, bedRecovering,
            player?.playWhenReady ?: false, player?.playbackState ?: Player.STATE_IDLE,
        )
        if (state == _bedState.value) return
        _bedState.value = state
        PlayerHooks.setBedState(state)
        if (state == BedState.Stopped) {
            clientLog.report("error", "bed_state", "sleep bed stopped", mapOf("url" to (bedUrl ?: "")))
        }
    }

    fun bedStop() {
        releaseBedPlayer()
    }

    /** #4018: the dialog's one-tap Restart bed: same bed, same volume. */
    fun restartBed() {
        val url = bedUrl ?: return
        bedPlay(url, bedTargetLevel.takeIf { it > 0f } ?: sleepBedLevel)
    }

    /** [volume] is plain player volume 0..1 (#5282): nothing goes past unity, the phone is the master. */
    fun bedVolume(volume: Float) {
        bedTargetLevel = volume.coerceIn(0f, 1f)
        applyBedLevel(bedTargetLevel)
    }

    private fun applyBedLevel(level: Float) {
        bedCurrentVolume = level.coerceIn(0f, 1f)
        bedPlayer?.volume = bedCurrentVolume
    }

    private fun disposeBedPlayer() {
        bedPlayer?.release()
        bedPlayer = null
    }

    /** #4018: the slider. Saved, and applied live unless a crossfade is still waiting to bring the bed up. */
    fun setSleepBedLevel(level: Float) {
        sleepBedLevel = level.coerceIn(0f, 1f)
        scope.launch { settingsStore.setSleepBedVolume(sleepBedLevel) }
        if (_sleepEndsAtMs.value != null) {
            if (sleepBedFadeTo != null) sleepBedFadeTo = sleepBedLevel
        } else if (bedWanted) {
            bedVolume(sleepBedLevel)
        }
    }

    /** #5282: "Book at bedtime". Saved, and applied live whenever the bedtime cut is in effect. */
    fun setSleepBookLevel(level: Float) {
        sleepBookLevel = level.coerceIn(0f, 1f)
        scope.launch { settingsStore.setSleepBookLevel(sleepBookLevel) }
        if (bedtimeTrimActive()) applyVolumeForCurrentKind()
    }

    /** #5282: the bed audible under the book while the dialog is open, so he balances by ear. */
    fun startMixPreview(bedUrl: String) {
        if (!_mixPreview.value) {
            recordSleepHold()  // he may raise the phone while listening
            previewResumedMain = _playerKind.value != null && !isPlaying.value
            _mixPreview.value = true
            if (previewResumedMain) resume()
            applyVolumeForCurrentKind(ramp = true)
        }
        bedPlay(SleepFade.resolveUrl(bedUrl, apiHolder.baseUrl), sleepBedLevel)
    }

    /** #5282: closing the dialog puts everything back: bed off, book as it was. No-op once Start took over. */
    fun stopMixPreview() {
        if (!_mixPreview.value) return
        _mixPreview.value = false
        bedStop()
        if (previewResumedMain) pause("mix_preview")
        applyVolumeForCurrentKind(ramp = true)
    }

    /** #4018: the bed to fall back to overnight; its local copy is fetched now, while the network is up. */
    fun setBedFallback(url: String?) {
        bedFallbackUrl = url
        if (url != null) cacheBed(url)
    }

    private fun bedCacheFile(url: String): File =
        File(File(context.filesDir, "sleep_beds"), SleepBedRules.cacheFileName(url))

    /** #4018: download the bed once; the next (re)start plays the local copy. */
    private fun cacheBed(url: String) {
        val file = bedCacheFile(url)
        if (file.exists() || !bedDownloads.add(file.name)) return
        scope.launch(Dispatchers.IO) {
            val result = runCatching {
                file.parentFile?.mkdirs()
                val part = File(file.parentFile, file.name + ".part")
                okHttpClient.newCall(Request.Builder().url(url).build()).execute().use { resp ->
                    check(resp.isSuccessful) { "HTTP ${resp.code}" }
                    part.outputStream().use { out -> resp.body!!.byteStream().copyTo(out) }
                }
                check(part.renameTo(file)) { "rename failed" }
                file.length()
            }
            withContext(Dispatchers.Main) { bedDownloads.remove(file.name) }
            result
                .onSuccess { clientLog.report("info", "bed_cached", "sleep bed saved locally", mapOf("url" to url, "bytes" to it.toString())) }
                .onFailure { clientLog.report("error", "bed_cache_failed", it.message ?: it.javaClass.simpleName, mapOf("url" to url)) }
        }
    }

    private fun releaseBedPlayer() {
        bedWanted = false
        bedRetryJob?.cancel()
        bedRetryJob = null
        bedRecovering = false
        disposeBedPlayer()
        bedCurrentVolume = 0f
        bedTargetLevel = 0f  // #5131
        refreshBedState()
        finishSleepHold()  // #5282
    }

    /**
     * Fade the MAIN player's volume to 0 over the last [fadeSeconds] of
     * [minutes] from now, then pause it — the "fade-out" layer (#1728). Only
     * touches [controller]/[setPlayerVolume]; the bed layer plays on
     * regardless. Superseded by a later call or [cancelSleepTimer].
     *
     * [bedFadeTo] (#3367 crossfade): when set, the bed ramps UP from its
     * current volume to this level over the same window, so the book fades
     * INTO the bed instead of into silence. Null keeps the bed untouched.
     */
    fun startSleepTimer(minutes: Float, fadeSeconds: Int, bedFadeTo: Float? = null) {
        sleepTimerJob?.cancel()
        sleepFadeSeconds = fadeSeconds
        sleepBedFadeTo = bedFadeTo
        _sleepEndsAtMs.value = System.currentTimeMillis() + (minutes * 60_000).toLong().coerceAtLeast(0)
        sleepFading = false
        mainFadedOut = false
        val wasArmed = sleepArmedWithBed
        sleepArmedWithBed = bedFadeTo != null  // #5282
        if (sleepArmedWithBed) recordSleepHold()
        if (sleepArmedWithBed || wasArmed) applyVolumeForCurrentKind(ramp = true)
        sleepTimerJob = scope.launch {
            val done = SleepFade.runTimer(  // #3953: the loop lives in SleepFade so it is unit-tested
                minutes, fadeSeconds, bedFadeTo,
                mainVolume = { controller?.volume },
                bedVolume = { if (bedWanted) bedCurrentVolume else 0f },
                setMain = { setPlayerVolume(it) },
                setBed = { if (bedWanted) bedVolume(it) },
                pause = {
                    PlayerHooks.notePause("sleep_timer")  // #3552
                    controller?.pause()
                },
                onFadeStart = {
                    mainRampJob?.cancel()
                    sleepFading = true  // #5282
                },
            )
            sleepFading = false
            sleepArmedWithBed = false  // #5282: from here the hold decides, at the next play
            mainFadedOut = done
            if (done) _sleepEndsAtMs.value = null
            if (done && bedFadeTo != null) {
                clientLog.report("info", "bed_handoff", "book faded into the sleep bed", volumeDetail())  // #5047
            }
        }
    }

    /** Cancel a pending fade-out and restore the current kind's configured dial. */
    fun cancelSleepTimer(keepBedtimeCut: Boolean = false) {
        sleepTimerJob?.cancel()
        sleepTimerJob = null
        _sleepEndsAtMs.value = null
        sleepFading = false  // #5282
        sleepArmedWithBed = keepBedtimeCut
        mainFadedOut = false
        finishSleepHold()
        applyVolumeForCurrentKind(ramp = true)
    }

    /**
     * The app's sleep button (#3714): keep the current book playing (resuming
     * it where it is if paused), then after [minutes] fade it out INTO [bedUrl]
     * (silent until the fade, then up to the bed slider's level, #4018), or into
     * silence when [bedUrl] is null. Same engine as dj_sleep_start.
     */
    fun startSleepMode(minutes: Float, bedUrl: String?, fadeSeconds: Int = SleepFade.DEFAULT_FADE_SECONDS) {
        _mixPreview.value = false  // #5282: Start takes over from Hear the mix
        if (bedUrl != null) {  // #5282: arm first, so a paused book never resumes at its day level
            sleepArmedWithBed = true
            recordSleepHold()
        }
        if (!isPlaying.value) resume()
        if (bedUrl != null) bedPlay(SleepFade.resolveUrl(bedUrl, apiHolder.baseUrl), 0f)
        startSleepTimer(minutes, fadeSeconds, if (bedUrl != null) sleepBedLevel else null)
    }

    /** Push the armed fade [minutes] later, undoing any fade already under way. */
    fun extendSleepMode(minutes: Float) {
        val endsAt = _sleepEndsAtMs.value ?: return
        val leftMin = (endsAt - System.currentTimeMillis()).coerceAtLeast(0) / 60_000f
        val bedTo = sleepBedFadeTo
        cancelSleepTimer(keepBedtimeCut = bedTo != null)  // #5282
        if (bedTo != null && bedWanted) bedVolume(0f)
        startSleepTimer(leftMin + minutes, sleepFadeSeconds, bedTo)
    }

    /** Cancel the sleep button's timer; a bed still waiting silently for the crossfade goes too. */
    fun cancelSleepMode() {
        cancelSleepTimer()
        if (bedWanted && bedTargetLevel == 0f) bedStop()  // #5131: the target, so a fade-in under way is not "waiting silently"
    }

    /** [source] says who asked (#3552): it rides the pause_source client log. */
    fun pause(source: String = "app") {
        PlayerHooks.notePause(source)
        controller?.pause()
    }

    /**
     * #3552: one `pause_source` line per pause. Whoever paused noted why in
     * [PlayerHooks]; otherwise the Media3 reason says it (headphones out, the
     * notification / lock screen / a Bluetooth remote).
     */
    private fun reportPauseSource(reason: Int) {
        val source = PlayerHooks.takePauseSource() ?: when (reason) {
            Player.PLAY_WHEN_READY_CHANGE_REASON_AUDIO_BECOMING_NOISY -> "headphones_disconnected"
            Player.PLAY_WHEN_READY_CHANGE_REASON_AUDIO_FOCUS_LOSS -> "audio_focus"
            Player.PLAY_WHEN_READY_CHANGE_REASON_END_OF_MEDIA_ITEM -> "stop_after_current"
            Player.PLAY_WHEN_READY_CHANGE_REASON_USER_REQUEST -> "media_session"
            else -> playWhenReadyReasonName(reason)
        }
        clientLog.report(
            level = "info",
            event = "pause_source",
            message = "paused by $source",
            detail = buildMap {
                put("source", source)
                put("reason", playWhenReadyReasonName(reason))
                currentTrackId()?.let { put("trackId", it.toString()) }
            },
        )
    }

    /** #3505: the DJ's exact stop at the end of the current song. */
    fun stopAfterCurrent(): Boolean = PlayerHooks.stopAfterCurrent()

    fun cancelStopAfterCurrent() = PlayerHooks.cancelStopAfterCurrent()

    fun resume() {
        // #4222: refresh the gain when a paused audiobook is activated again.
        if (_playerKind.value == PlayerKind.Audiobook) {
            _currentBook.value?.let { fetchBookGain(it.id) }
        }
        applyVolumeForCurrentKind()  // #5282: after a sleep fade the player sits at 0; don't wait on the network
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

    fun skipForward(stepMs: Long = 30_000) {  // #4220: the step is Todd's pick
        if (isMusic()) {
            controller?.seekToNextMediaItem()
            return
        }
        val target = (_positionMs.value + stepMs).coerceAtMost(_durationMs.value)
        seekToGlobalMs(target)
        _positionMs.value = target
    }

    fun skipBack(stepMs: Long = 30_000) {  // #4220
        if (isMusic()) {
            controller?.seekToPreviousMediaItem()
            return
        }
        val target = (_positionMs.value - stepMs).coerceAtLeast(0)
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
        seekControllerTo(globalPositionMs)
        // #4219: the position loop only runs while playing, so a paused skip would leave
        // the chapter-relative seek bar on the old chapter.
        _positionMs.value = globalPositionMs.coerceAtLeast(0)
        updateCurrentChapter()
    }

    private fun seekControllerTo(globalPositionMs: Long) {
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
        resetBookGain()  // #4222
        if (_currentBook.value != null) {
            scope.launch { syncProgress() }
        }
        if (_currentMusic.value != null) {
            finalizeMusicIfActive()
        }
        progressSyncJob?.cancel()
        positionUpdateJob?.cancel()
        sleepTimerJob?.cancel()
        releaseBedPlayer()
        controller?.removeListener(playerListener)
        controllerFuture?.let { MediaController.releaseFuture(it) }
        controller = null
    }
}

// #5047: AudioManager's hidden volume broadcast and its extras (stable strings since API 1).
private const val VOLUME_CHANGED_ACTION = "android.media.VOLUME_CHANGED_ACTION"
private const val EXTRA_VOLUME_STREAM_TYPE = "android.media.EXTRA_VOLUME_STREAM_TYPE"
private const val EXTRA_VOLUME_STREAM_VALUE = "android.media.EXTRA_VOLUME_STREAM_VALUE"
private const val EXTRA_PREV_VOLUME_STREAM_VALUE = "android.media.EXTRA_PREV_VOLUME_STREAM_VALUE"
private const val VOLUME_BURST_MS = 2_000L
