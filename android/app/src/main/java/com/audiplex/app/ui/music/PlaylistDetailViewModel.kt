package com.audiplex.app.ui.music

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.FavoritesStore
import com.audiplex.app.data.MusicActions
import com.audiplex.app.data.api.PlaylistDetail
import com.audiplex.app.data.api.PlaylistSummary
import com.audiplex.app.playback.PlaybackManager
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

sealed class PlaylistDetailUiState {
    data object Loading : PlaylistDetailUiState()
    data class Success(
        val playlist: PlaylistDetail,
        val albumLookup: Map<Int, Pair<String, Boolean>>
    ) : PlaylistDetailUiState()
    data class Error(val message: String) : PlaylistDetailUiState()
}

@HiltViewModel
class PlaylistDetailViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val playbackManager: PlaybackManager,
    val favoritesStore: FavoritesStore,
    val musicActions: MusicActions
) : ViewModel() {

    private val _uiState = MutableStateFlow<PlaylistDetailUiState>(PlaylistDetailUiState.Loading)
    val uiState: StateFlow<PlaylistDetailUiState> = _uiState

    val favoritesByType = favoritesStore.byType

    private val _playlists = MutableStateFlow<List<PlaylistSummary>>(emptyList())
    val playlists: StateFlow<List<PlaylistSummary>> = _playlists

    fun load(playlistId: Int) {
        viewModelScope.launch {
            _uiState.value = PlaylistDetailUiState.Loading
            try {
                val api = apiHolder.api ?: throw IllegalStateException("No server configured")
                val playlist = api.getPlaylist(playlistId)
                val albumLookup = try {
                    api.getAlbums().associate { it.id to (it.title to it.hasCover) }
                } catch (_: Exception) {
                    emptyMap()
                }
                _uiState.value = PlaylistDetailUiState.Success(playlist, albumLookup)
                _playlists.value = musicActions.loadPlaylists()
            } catch (e: Exception) {
                _uiState.value = PlaylistDetailUiState.Error(e.message ?: "Failed to load playlist")
            }
        }
    }

    fun playPlaylist(
        playlist: PlaylistDetail,
        albumLookup: Map<Int, Pair<String, Boolean>>,
        startIndex: Int = 0
    ) {
        playbackManager.playPlaylist(playlist, apiHolder.baseUrl, albumLookup, startIndex)
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
