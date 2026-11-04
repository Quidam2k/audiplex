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
import com.audiplex.app.playback.ORIGIN_MANUAL
import com.audiplex.app.playback.PlaybackManager
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.Job
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
    val musicActions: MusicActions,
    private val playbackManager: PlaybackManager // #4029
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

    // #4029: one folder load at a time; a newer one cancels the older, so a
    // stale listing can never land on top of the folder you moved to.
    private var folderJob: Job? = null

    private fun loadFolder(path: String?) {
        folderJob?.cancel()
        folderJob = viewModelScope.launch {
            val api = apiHolder.api ?: return@launch
            // Keep the current listing on failure — nulling it would strand the
            // Folders tab on a spinner (its load-once effect won't re-fire).
            runCatching { api.getFolders(path) }
                .onSuccess { _folderListing.value = it }
        }
    }

    fun openFolderRoot() = loadFolder(null)

    fun enterFolder(path: String) = loadFolder(path)

    /**
     * Go up one level using the listing's parent pointer (null = roots).
     * #4029: ignored while a folder is still loading, so a fast double back
     * can't compute "up" from the folder you are already leaving.
     */
    fun exitFolder() {
        if (folderJob?.isActive == true) return
        loadFolder(_folderListing.value?.parent)
    }

    /** #4029: play everything under [path] (all subfolders), shuffled. */
    fun shuffleFolder(path: String, name: String) {
        viewModelScope.launch {
            try {
                val api = apiHolder.api ?: return@launch
                val tracks = api.getFolderTracks(path)
                if (tracks.isEmpty()) return@launch
                val albumLookup = (_uiState.value as? MusicUiState.Success)
                    ?.albums.orEmpty()
                    .associate { it.id to (it.title to it.hasCover) }
                playbackManager.playTracks(
                    tracks = tracks,
                    baseUrl = apiHolder.baseUrl,
                    title = name,
                    albumLookup = albumLookup,
                    shuffle = true,
                    origin = ORIGIN_MANUAL
                )
            } catch (_: Exception) { }
        }
    }

    fun getBaseUrl(): String = apiHolder.baseUrl
}
