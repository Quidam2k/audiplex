package com.audiplex.app.ui.music

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.FavoritesStore
import com.audiplex.app.data.MusicActions
import com.audiplex.app.data.api.AlbumDetail
import com.audiplex.app.data.api.PlaylistSummary
import com.audiplex.app.playback.PlaybackManager
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

sealed class AlbumDetailUiState {
    data object Loading : AlbumDetailUiState()
    data class Success(val album: AlbumDetail) : AlbumDetailUiState()
    data class Error(val message: String) : AlbumDetailUiState()
}

@HiltViewModel
class AlbumDetailViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val playbackManager: PlaybackManager,
    val favoritesStore: FavoritesStore,
    val musicActions: MusicActions
) : ViewModel() {

    private val _uiState = MutableStateFlow<AlbumDetailUiState>(AlbumDetailUiState.Loading)
    val uiState: StateFlow<AlbumDetailUiState> = _uiState

    val favoritesByType = favoritesStore.byType

    private val _playlists = MutableStateFlow<List<PlaylistSummary>>(emptyList())
    val playlists: StateFlow<List<PlaylistSummary>> = _playlists

    fun load(albumId: Int) {
        viewModelScope.launch {
            _uiState.value = AlbumDetailUiState.Loading
            try {
                val api = apiHolder.api ?: throw IllegalStateException("No server configured")
                val album = api.getAlbum(albumId)
                _uiState.value = AlbumDetailUiState.Success(album)
                _playlists.value = musicActions.loadPlaylists()
            } catch (e: Exception) {
                _uiState.value = AlbumDetailUiState.Error(e.message ?: "Failed to load album")
            }
        }
    }

    fun playAlbum(album: AlbumDetail, startIndex: Int = 0) {
        playbackManager.playAlbum(album, apiHolder.baseUrl, startIndex)
    }

    fun shuffleAlbum(album: AlbumDetail) {
        playbackManager.playAlbum(album, apiHolder.baseUrl, startIndex = 0, shuffle = true)
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
