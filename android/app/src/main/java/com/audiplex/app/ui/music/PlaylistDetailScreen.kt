package com.audiplex.app.ui.music

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.audiplex.app.data.api.TrackSchema
import com.audiplex.app.ui.common.AddToPlaylistButton
import com.audiplex.app.ui.common.SearchTopBar
import com.audiplex.app.ui.common.ItemActionSheet
import com.audiplex.app.ui.common.ItemKind
import com.audiplex.app.ui.common.NewPlaylistDialog
import com.audiplex.app.ui.common.PlaylistPickerSheet

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PlaylistDetailScreen(
    playlistId: Int,
    onBack: () -> Unit,
    onPlayerClick: () -> Unit,
    viewModel: PlaylistDetailViewModel = hiltViewModel()
) {
    val uiState by viewModel.uiState.collectAsState()
    val favoritesByType by viewModel.favoritesByType.collectAsState()
    val playlists by viewModel.playlists.collectAsState()

    var actionTrack by remember { mutableStateOf<TrackSchema?>(null) }
    var pickerForTrack by remember { mutableStateOf<TrackSchema?>(null) }
    var newPlaylistForTrack by remember { mutableStateOf<TrackSchema?>(null) }
    var searchQuery by rememberSaveable { mutableStateOf("") }

    LaunchedEffect(playlistId) {
        viewModel.load(playlistId)
    }

    Column(modifier = Modifier.fillMaxSize()) {
        val titleText = (uiState as? PlaylistDetailUiState.Success)?.playlist?.name ?: "Playlist"
        SearchTopBar(
            title = titleText,
            searchQuery = searchQuery,
            onSearchQueryChange = { searchQuery = it },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                }
            },
            actions = {
                (uiState as? PlaylistDetailUiState.Success)?.playlist?.let { playlist ->
                    AddToPlaylistButton(
                        entityType = "playlist",
                        entityKey = playlist.id.toString(),
                        suggestedName = playlist.name
                    )
                }
            }
        )

        when (val state = uiState) {
            is PlaylistDetailUiState.Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)
            }
            is PlaylistDetailUiState.Error -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(
                    text = state.message,
                    color = MaterialTheme.colorScheme.error,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(32.dp)
                )
            }
            is PlaylistDetailUiState.Success -> PlaylistContent(
                state = state,
                query = searchQuery,
                favoriteTracks = favoritesByType["track"].orEmpty(),
                onPlay = {
                    viewModel.playPlaylist(state.playlist, state.albumLookup, 0)
                    onPlayerClick()
                },
                onTrackClick = { idx ->
                    viewModel.playPlaylist(state.playlist, state.albumLookup, idx)
                    onPlayerClick()
                },
                onTrackLongPress = { actionTrack = it }
            )
        }
    }

    actionTrack?.let { track ->
        val key = track.id.toString()
        ItemActionSheet(
            title = track.title,
            subtitle = track.artistName,
            kind = ItemKind.Track,
            isFavorited = key in favoritesByType["track"].orEmpty(),
            onDismiss = { actionTrack = null },
            onToggleFavorite = { viewModel.toggleFavorite("track", key) },
            onAddToPlaylist = { pickerForTrack = track },
            onCreatePlaylist = { newPlaylistForTrack = track }
        )
    }

    pickerForTrack?.let { track ->
        PlaylistPickerSheet(
            playlists = playlists,
            onDismiss = { pickerForTrack = null },
            onPick = { playlist ->
                viewModel.addEntityToPlaylist("track", track.id.toString(), playlist.id)
            },
            onCreateNew = { newPlaylistForTrack = track }
        )
    }

    newPlaylistForTrack?.let { track ->
        NewPlaylistDialog(
            suggestedName = track.title,
            onDismiss = { newPlaylistForTrack = null },
            onConfirm = { name ->
                viewModel.createPlaylistFromEntity("track", track.id.toString(), name)
                newPlaylistForTrack = null
            }
        )
    }
}

@Composable
private fun PlaylistContent(
    state: PlaylistDetailUiState.Success,
    query: String,
    favoriteTracks: Set<String>,
    onPlay: () -> Unit,
    onTrackClick: (Int) -> Unit,
    onTrackLongPress: (TrackSchema) -> Unit
) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        item {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(state.playlist.name, style = MaterialTheme.typography.titleLarge)
                Text(
                    text = "${state.playlist.trackCount} tracks",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }

        item {
            Button(
                onClick = onPlay,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp)
            ) {
                Icon(Icons.Default.PlayArrow, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Play playlist")
            }
        }

        if (state.playlist.tracks.isNotEmpty()) {
            // Filter by title while preserving each track's ORIGINAL index —
            // playback seeks by position into the full list.
            val visibleTracks = state.playlist.tracks.withIndex()
                .filter { it.value.title.contains(query, ignoreCase = true) }

            item {
                Text(
                    "Tracks",
                    style = MaterialTheme.typography.titleMedium,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp)
                )
            }

            itemsIndexed(visibleTracks) { pos, entry ->
                val (index, track) = entry
                TrackRow(
                    index = index + 1,
                    track = track,
                    isFavorite = track.id.toString() in favoriteTracks,
                    onClick = { onTrackClick(index) },
                    onLongClick = { onTrackLongPress(track) }
                )
                if (pos < visibleTracks.size - 1) {
                    HorizontalDivider(
                        modifier = Modifier.padding(horizontal = 16.dp),
                        color = MaterialTheme.colorScheme.surfaceVariant
                    )
                }
            }
        }

        item { Spacer(Modifier.height(80.dp)) }
    }
}
