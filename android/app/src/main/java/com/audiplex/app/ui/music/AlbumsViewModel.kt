package com.audiplex.app.ui.music

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.FavoritesStore
import com.audiplex.app.data.api.AlbumSummary
import com.audiplex.app.playback.PlaybackManager
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

sealed class AlbumsUiState {
    data object Loading : AlbumsUiState()
    data class Success(
        val title: String,
        val genre: String?,
        val albums: List<AlbumSummary>
    ) : AlbumsUiState()
    data class Error(val message: String) : AlbumsUiState()
}

@HiltViewModel
class AlbumsViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val playbackManager: PlaybackManager,
    val favoritesStore: FavoritesStore
) : ViewModel() {

    private val _uiState = MutableStateFlow<AlbumsUiState>(AlbumsUiState.Loading)
    val uiState: StateFlow<AlbumsUiState> = _uiState

    val favoritesByType = favoritesStore.byType

    fun load(genre: String? = null, artistId: Int? = null, title: String) {
        viewModelScope.launch {
            _uiState.value = AlbumsUiState.Loading
            try {
                val api = apiHolder.api ?: throw IllegalStateException("No server configured")
                val albums = api.getAlbums(genre = genre, artistId = artistId)
                _uiState.value = AlbumsUiState.Success(title = title, genre = genre, albums = albums)
            } catch (e: Exception) {
                _uiState.value = AlbumsUiState.Error(e.message ?: "Failed to load albums")
            }
        }
    }

    fun playGenre(genre: String, shuffle: Boolean, onStarted: () -> Unit = {}) {
        viewModelScope.launch {
            try {
                val api = apiHolder.api ?: return@launch
                val tracks = api.getGenreTracks(genre)
                if (tracks.isEmpty()) return@launch
                val albumLookup = (_uiState.value as? AlbumsUiState.Success)
                    ?.albums.orEmpty()
                    .associate { it.id to (it.title to it.hasCover) }
                playbackManager.playTracks(
                    tracks = tracks,
                    baseUrl = apiHolder.baseUrl,
                    title = genre,
                    albumLookup = albumLookup,
                    shuffle = shuffle
                )
                onStarted()
            } catch (_: Exception) { }
        }
    }

    fun toggleFavorite(type: String, key: String) {
        viewModelScope.launch { favoritesStore.toggle(type, key) }
    }

    fun getBaseUrl(): String = apiHolder.baseUrl
}
