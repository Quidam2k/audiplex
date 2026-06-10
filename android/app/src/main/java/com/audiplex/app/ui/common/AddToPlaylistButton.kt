package com.audiplex.app.ui.common

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.PlaylistAdd
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.MusicActions
import com.audiplex.app.data.api.PlaylistSummary
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

/**
 * Self-contained "add to playlist" action: an icon button that opens the
 * playlist picker, resolving the entity (album / artist / genre / folder /
 * track / playlist) to its tracks and appending — or creating a new
 * playlist from it. Drop it into any top bar or row; it carries its own VM
 * so the host screen doesn't have to wire playlist plumbing.
 */
@HiltViewModel
class AddToPlaylistViewModel @Inject constructor(
    private val musicActions: MusicActions
) : ViewModel() {

    private val _playlists = MutableStateFlow<List<PlaylistSummary>>(emptyList())
    val playlists: StateFlow<List<PlaylistSummary>> = _playlists

    fun loadPlaylists() {
        viewModelScope.launch { _playlists.value = musicActions.loadPlaylists() }
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
}

@Composable
fun AddToPlaylistButton(
    entityType: String,
    entityKey: String,
    suggestedName: String,
    viewModel: AddToPlaylistViewModel = hiltViewModel()
) {
    var showPicker by remember { mutableStateOf(false) }
    var showNewDialog by remember { mutableStateOf(false) }
    val playlists by viewModel.playlists.collectAsState()

    IconButton(
        onClick = {
            viewModel.loadPlaylists()
            showPicker = true
        }
    ) {
        Icon(
            Icons.AutoMirrored.Filled.PlaylistAdd,
            contentDescription = "Add to playlist"
        )
    }

    if (showPicker) {
        PlaylistPickerSheet(
            playlists = playlists,
            onDismiss = { showPicker = false },
            onPick = { playlist ->
                viewModel.addEntityToPlaylist(entityType, entityKey, playlist.id)
            },
            onCreateNew = { showNewDialog = true }
        )
    }

    if (showNewDialog) {
        NewPlaylistDialog(
            suggestedName = suggestedName,
            onDismiss = { showNewDialog = false },
            onConfirm = { name ->
                viewModel.createPlaylistFromEntity(entityType, entityKey, name)
                showNewDialog = false
            }
        )
    }
}
