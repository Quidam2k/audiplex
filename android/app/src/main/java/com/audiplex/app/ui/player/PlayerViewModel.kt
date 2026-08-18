package com.audiplex.app.ui.player

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.api.BookDetail
import com.audiplex.app.data.api.ChapterSchema
import com.audiplex.app.data.api.TrackRatingCreate
import com.audiplex.app.playback.MusicQueueState
import com.audiplex.app.playback.PlaybackManager
import com.audiplex.app.playback.PlayerKind
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class PlayerViewModel @Inject constructor(
    private val playbackManager: PlaybackManager,
    private val apiHolder: ApiServiceHolder
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
    private val _ratings = MutableStateFlow<Map<Int, Int>>(emptyMap())
    val ratings: StateFlow<Map<Int, Int>> = _ratings.asStateFlow()

    init {
        viewModelScope.launch {
            val api = apiHolder.api ?: return@launch
            runCatching { api.getTrackRatings() }
                .onSuccess { loaded -> _ratings.value = loaded.associate { it.trackId to it.rating } }
        }
    }

    /** Tap a star to set it; tap the star that is already set to clear. */
    fun rateTrack(trackId: Int, stars: Int) {
        val clearing = _ratings.value[trackId] == stars
        _ratings.value = _ratings.value.toMutableMap().apply {
            if (clearing) remove(trackId) else put(trackId, stars)
        }
        viewModelScope.launch {
            val api = apiHolder.api ?: return@launch
            runCatching {
                if (clearing) api.clearTrackRating(trackId)
                else api.setTrackRating(trackId, TrackRatingCreate(rating = stars))
            }
        }
    }

    val hasActiveBook: StateFlow<Boolean> = combine(
        playbackManager.currentBook,
        playbackManager.currentMusic,
        playbackManager.currentStreamTitle
    ) { book, music, streamTitle -> book != null || music != null || streamTitle != null }
        .stateIn(
            scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Main),
            started = kotlinx.coroutines.flow.SharingStarted.WhileSubscribed(5000),
            initialValue = false
        )

    fun togglePlayPause() {
        if (isPlaying.value) {
            playbackManager.pause()
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
