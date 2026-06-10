package com.audiplex.app.ui.music

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.FavoritesStore
import com.audiplex.app.data.MusicActions
import com.audiplex.app.data.api.MusicArtistDetail
import com.audiplex.app.data.api.PlaylistSummary
import com.audiplex.app.playback.PlaybackManager
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

sealed class ArtistDetailUiState {
    data object Loading : ArtistDetailUiState()
    data class Success(val artist: MusicArtistDetail) : ArtistDetailUiState()
    data class Error(val message: String) : ArtistDetailUiState()
}

@HiltViewModel
class ArtistDetailViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val playbackManager: PlaybackManager,
    val favoritesStore: FavoritesStore,
    val musicActions: MusicActions
) : ViewModel() {

    private val _uiState = MutableStateFlow<ArtistDetailUiState>(ArtistDetailUiState.Loading)
    val uiState: StateFlow<ArtistDetailUiState> = _uiState

    val favoritesByType = favoritesStore.byType

    private val _playlists = MutableStateFlow<List<PlaylistSummary>>(emptyList())
    val playlists: StateFlow<List<PlaylistSummary>> = _playlists

    fun load(artistId: Int) {
        viewModelScope.launch {
            _uiState.value = ArtistDetailUiState.Loading
            try {
                val api = apiHolder.api ?: throw IllegalStateException("No server configured")
                val artist = api.getArtist(artistId)
                _uiState.value = ArtistDetailUiState.Success(artist)
                _playlists.value = musicActions.loadPlaylists()
            } catch (e: Exception) {
                _uiState.value = ArtistDetailUiState.Error(e.message ?: "Failed to load artist")
            }
        }
    }

    fun playAll(shuffle: Boolean, onStarted: () -> Unit = {}) {
        val artist = (uiState.value as? ArtistDetailUiState.Success)?.artist ?: return
        viewModelScope.launch {
            try {
                val api = apiHolder.api ?: return@launch
                val tracks = api.getArtistTracks(artist.id)
                if (tracks.isEmpty()) return@launch
                val albumLookup = artist.albums.associate {
                    it.id to (it.title to it.hasCover)
                }
                playbackManager.playTracks(
                    tracks = tracks,
                    baseUrl = apiHolder.baseUrl,
                    title = artist.name,
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

    fun addEntityToPlaylist(entityKind: String, entityKey: String, playlistId: Int) {
        viewModelScope.launch {
            val trackIds = musicActions.resolveTrackIds(entityKind, entityKey)
            if (trackIds.isNotEmpty()) {
                musicActions.appendTrackIds(playlistId, trackIds)
                _playlists.value = musicActions.loadPlaylists()
            }
        }
    }

    fun createPlaylistFromEntity(entityKind: String, entityKey: String, name: String) {
        viewModelScope.launch {
            val trackIds = musicActions.resolveTrackIds(entityKind, entityKey)
            musicActions.createWithTracks(name, trackIds)
            _playlists.value = musicActions.loadPlaylists()
        }
    }

    fun getBaseUrl(): String = apiHolder.baseUrl
}
