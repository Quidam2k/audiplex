package com.audiplex.app.ui.player

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.data.api.BookDetail
import com.audiplex.app.data.api.ChapterSchema
import com.audiplex.app.data.api.SleepBed
import com.audiplex.app.data.api.TrackRatingCreate
import com.audiplex.app.playback.ControllerMetadata
import com.audiplex.app.playback.MusicQueueState
import com.audiplex.app.playback.PlaybackManager
import com.audiplex.app.playback.PlayerKind
import com.audiplex.app.playback.SleepFade
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.delay
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class PlayerViewModel @Inject constructor(
    private val playbackManager: PlaybackManager,
    private val apiHolder: ApiServiceHolder,
    private val settingsStore: SettingsStore,
) : ViewModel() {

    val currentBook: StateFlow<BookDetail?> = playbackManager.currentBook
    val currentMusic: StateFlow<MusicQueueState?> = playbackManager.currentMusic
    val currentStreamTitle: StateFlow<String?> = playbackManager.currentStreamTitle
    val playerKind: StateFlow<PlayerKind?> = playbackManager.playerKind
    val isPlaying: StateFlow<Boolean> = playbackManager.isPlaying
    val positionMs: StateFlow<Long> = playbackManager.positionMs
    val durationMs: StateFlow<Long> = playbackManager.durationMs
    val currentChapterIndex: StateFlow<Int> = playbackManager.currentChapterIndex

    /**
     * Star ratings by track id (#3024).
     *
     * Held locally and updated optimistically so a tap lands instantly — the
     * point of putting this on the now-playing screen is that Todd rates a
     * track while it is playing, and a control that waits on a round trip
     * over Tailscale before it moves does not get used.
     */
    // #6117: halves, so Double (4.5).
    private val _ratings = MutableStateFlow<Map<Int, Double>>(emptyMap())
    val ratings: StateFlow<Map<Int, Double>> = _ratings.asStateFlow()

    init {
        // Adopt an in-progress session's now-playing state the moment the UI
        // exists, so walking up to a screen-off DJ session shows the track and
        // controls instead of a blank player (#3105/#993). Idempotent.
        playbackManager.connect()
        viewModelScope.launch {
            val api = apiHolder.api ?: return@launch
            runCatching { api.getTrackRatings() }
                .onSuccess { loaded ->
                    _ratings.value = loaded.associate { it.trackId to (it.stars ?: it.rating.toDouble()) }  // #6117
                }
        }
    }

    /** Tap a star to set it; tap the star that is already set to clear. */
    fun rateTrack(trackId: Int, stars: Double) {  // #6117: halves
        val next = nextRating(_ratings.value[trackId], stars)
        val clearing = next == null
        _ratings.value = _ratings.value.toMutableMap().apply {
            if (next == null) remove(trackId) else put(trackId, next)
        }
        viewModelScope.launch {
            val api = apiHolder.api ?: return@launch
            runCatching {
                if (clearing) api.clearTrackRating(trackId)
                else api.setTrackRating(trackId, TrackRatingCreate(stars = stars))  // #6117
            }
        }
    }

    /** #3505: the controller's own title/artist, for when local state is missing. */
    val controllerMetadata: StateFlow<ControllerMetadata?> = playbackManager.controllerMetadata

    // #3505: anything the session is playing counts, not only what this
    // process loaded itself: DJ-driven playback must always show controls.
    val hasActiveBook: StateFlow<Boolean> = combine(
        playbackManager.currentBook,
        playbackManager.currentMusic,
        playbackManager.currentStreamTitle,
        playbackManager.controllerMetadata
    ) { book, music, streamTitle, meta ->
        book != null || music != null || streamTitle != null || meta != null
    }
        .stateIn(
            scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Main),
            started = kotlinx.coroutines.flow.SharingStarted.WhileSubscribed(5000),
            initialValue = false
        )

    fun togglePlayPause() {
        if (isPlaying.value) {
            playbackManager.pause("app_ui")  // #3552
        } else {
            playbackManager.resume()
        }
    }

    fun seekTo(positionMs: Long) {
        playbackManager.seekTo(positionMs)
    }

    fun skipForward() {
        playbackManager.skipForward()
    }

    fun skipBack() {
        playbackManager.skipBack()
    }

    fun nextChapter() {
        playbackManager.nextChapter()
    }

    fun previousChapter() {
        playbackManager.previousChapter()
    }

    fun seekToChapter(index: Int) {
        playbackManager.seekToChapter(index)
    }

    fun seekToTrack(index: Int) {
        playbackManager.seekToTrack(index)
    }

    fun getCurrentChapter(): ChapterSchema? {
        val book = currentBook.value ?: return null
        return book.chapters.getOrNull(currentChapterIndex.value)
    }

    fun getBaseUrl(): String = apiHolder.baseUrl

    // ----- #3714: the sleep button -----

    val bedPlaying: StateFlow<Boolean> = playbackManager.bedPlaying

    private val _sleepBeds = MutableStateFlow<List<SleepBed>>(emptyList())
    val sleepBeds: StateFlow<List<SleepBed>> = _sleepBeds.asStateFlow()

    /** Minutes until the armed sleep fade starts, ticking each 15 s; null when none is armed. */
    val sleepMinutesLeft: StateFlow<Int?> = combine(
        playbackManager.sleepEndsAtMs,
        kotlinx.coroutines.flow.flow { while (currentCoroutineContext().isActive) { emit(Unit); delay(15_000) } }
    ) { endsAt, _ -> endsAt?.let { SleepFade.minutesLeft(it, System.currentTimeMillis()) } }
        .stateIn(viewModelScope, kotlinx.coroutines.flow.SharingStarted.WhileSubscribed(5000), null)

    // #3953: why the bed list did not load, null when it did. The dialog shows it
    // instead of silently offering only Silence (10/6: a 404 left Todd in silence).
    private val _sleepBedsError = MutableStateFlow<String?>(null)
    val sleepBedsError: StateFlow<String?> = _sleepBedsError.asStateFlow()

    /** #3953: the bed the dialog preselects: last used if still offered, else the first. */
    val defaultSleepBedId: StateFlow<Int?> = combine(_sleepBeds, settingsStore.lastSleepBedId) { beds, last ->
        SleepFade.defaultBedId(beds.map { it.id }, last)
    }.stateIn(viewModelScope, kotlinx.coroutines.flow.SharingStarted.WhileSubscribed(5000), null)

    /** Refresh the bed list when the dialog opens: a bed dropped into the library shows up. */
    fun loadSleepBeds() {
        viewModelScope.launch {
            val api = apiHolder.api
            if (api == null) {
                _sleepBedsError.value = "no server configured"
                return@launch
            }
            runCatching { api.getSleepBeds() }
                .onSuccess { _sleepBeds.value = it; _sleepBedsError.value = null }
                .onFailure { _sleepBedsError.value = it.message ?: it.javaClass.simpleName }
        }
    }

    fun startSleep(minutes: Int, bed: SleepBed?, fadeSeconds: Int = SleepFade.DEFAULT_FADE_SECONDS) {
        if (bed != null) viewModelScope.launch { settingsStore.setLastSleepBedId(bed.id) }
        playbackManager.startSleepMode(minutes.toFloat(), bed?.streamUrl, fadeSeconds)
    }
    fun extendSleep() = playbackManager.extendSleepMode(15f)
    fun cancelSleep() = playbackManager.cancelSleepMode()
    fun stopSleepBed() = playbackManager.bedStop()

    fun formatTime(ms: Long): String {
        val totalSeconds = ms / 1000
        val hours = totalSeconds / 3600
        val minutes = (totalSeconds % 3600) / 60
        val seconds = totalSeconds % 60
        return if (hours > 0) {
            "%d:%02d:%02d".format(hours, minutes, seconds)
        } else {
            "%d:%02d".format(minutes, seconds)
        }
    }
}
