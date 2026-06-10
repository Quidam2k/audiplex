package com.audiplex.app.ui.music

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.FavoritesStore
import com.audiplex.app.data.MusicActions
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.data.api.AlbumSummary
import com.audiplex.app.data.api.FolderListing
import com.audiplex.app.data.api.GenreSchema
import com.audiplex.app.data.api.MusicArtistSchema
import com.audiplex.app.data.api.PlaylistSummary
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch
import javax.inject.Inject

sealed class MusicUiState {
    data object Loading : MusicUiState()
    data object NoServer : MusicUiState()
    data class Success(
        val genres: List<GenreSchema>,
        val artists: List<MusicArtistSchema>,
        val albums: List<AlbumSummary>,
        val playlists: List<PlaylistSummary>
    ) : MusicUiState()
    data class Error(val message: String) : MusicUiState()
}

@HiltViewModel
class MusicViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val settingsStore: SettingsStore,
    val favoritesStore: FavoritesStore,
    val musicActions: MusicActions
) : ViewModel() {

    private val _uiState = MutableStateFlow<MusicUiState>(MusicUiState.Loading)
    val uiState: StateFlow<MusicUiState> = _uiState

    val favoritesByType = favoritesStore.byType

    private val _isRefreshing = MutableStateFlow(false)
    val isRefreshing: StateFlow<Boolean> = _isRefreshing

    private val _favoritesOnly = MutableStateFlow(false)
    val favoritesOnly: StateFlow<Boolean> = _favoritesOnly

    // Folder-browsing state. `null` = not loaded yet; the Folders tab loads
    // the roots on first open. "Up" navigation rides the server-provided
    // `parent` pointer, so no separate path stack is needed.
    private val _folderListing = MutableStateFlow<FolderListing?>(null)
    val folderListing: StateFlow<FolderListing?> = _folderListing

    init {
        viewModelScope.launch {
            settingsStore.serverUrl.distinctUntilChanged().collect { url ->
                if (url.isBlank()) {
                    _uiState.value = MusicUiState.NoServer
                } else {
                    apiHolder.setBaseUrl(url)
                    load()
                }
            }
        }
    }

    fun load() {
        viewModelScope.launch {
            _uiState.value = MusicUiState.Loading
            doLoad()
        }
    }

    private suspend fun doLoad() {
        try {
            val api = apiHolder.api ?: run {
                _uiState.value = MusicUiState.NoServer
                return
            }
            val genres = api.getGenres()
            val artists = api.getMusicArtists()
            val albums = api.getAlbums()
            val playlists = try { api.getPlaylists() } catch (_: Exception) { emptyList() }
            _uiState.value = MusicUiState.Success(genres, artists, albums, playlists)
            favoritesStore.refresh()
        } catch (e: Exception) {
            _uiState.value = MusicUiState.Error(e.message ?: "Failed to load music")
        }
    }

    fun refreshLibrary() {
        viewModelScope.launch {
            _isRefreshing.value = true
            try {
                apiHolder.api?.scanLibrary()
            } catch (_: Exception) { }
            doLoad()
            _isRefreshing.value = false
        }
    }

    fun setFavoritesOnly(value: Boolean) {
        _favoritesOnly.value = value
    }

    fun toggleFavorite(entityType: String, entityKey: String) {
        viewModelScope.launch { favoritesStore.toggle(entityType, entityKey) }
    }

    fun addEntityToPlaylist(entityKind: String, entityKey: String, playlistId: Int) {
        viewModelScope.launch {
            val trackIds = musicActions.resolveTrackIds(entityKind, entityKey)
            if (trackIds.isNotEmpty()) {
                musicActions.appendTrackIds(playlistId, trackIds)
                load()  // refresh playlist counts
            }
        }
    }

    fun createPlaylistFromEntity(entityKind: String, entityKey: String, name: String) {
        viewModelScope.launch {
            val trackIds = musicActions.resolveTrackIds(entityKind, entityKey)
            musicActions.createWithTracks(name, trackIds)
            load()
        }
    }

    fun openFolderRoot() {
        viewModelScope.launch {
            val api = apiHolder.api ?: return@launch
            // Keep the current listing on failure — nulling it would strand the
            // Folders tab on a spinner (its load-once effect won't re-fire).
            runCatching { api.getFolders(null) }
                .onSuccess { _folderListing.value = it }
        }
    }

    fun enterFolder(path: String) {
        viewModelScope.launch {
            val api = apiHolder.api ?: return@launch
            runCatching { api.getFolders(path) }
                .onSuccess { _folderListing.value = it }
        }
    }

    /** Go up one level using the listing's parent pointer (null = roots). */
    fun exitFolder() {
        val parent = _folderListing.value?.parent
        if (parent == null) openFolderRoot() else enterFolder(parent)
    }

    fun getBaseUrl(): String = apiHolder.baseUrl
}
