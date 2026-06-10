package com.audiplex.app.ui.music

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Shuffle
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
import com.audiplex.app.data.api.AlbumSummary
import com.audiplex.app.data.api.MusicArtistDetail
import com.audiplex.app.ui.common.AddToPlaylistButton
import com.audiplex.app.ui.common.SearchTopBar
import com.audiplex.app.ui.common.ItemActionSheet
import com.audiplex.app.ui.common.ItemKind
import com.audiplex.app.ui.common.NewPlaylistDialog
import com.audiplex.app.ui.common.PlaylistPickerSheet

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ArtistDetailScreen(
    artistId: Int,
    onBack: () -> Unit,
    onAlbumClick: (Int) -> Unit,
    onPlayerClick: () -> Unit,
    viewModel: ArtistDetailViewModel = hiltViewModel()
) {
    val uiState by viewModel.uiState.collectAsState()
    val favoritesByType by viewModel.favoritesByType.collectAsState()
    val playlists by viewModel.playlists.collectAsState()

    var actionTarget by remember { mutableStateOf<Any?>(null) }  // MusicArtistDetail or AlbumSummary
    var pickerTarget by remember { mutableStateOf<Any?>(null) }
    var newPlaylistTarget by remember { mutableStateOf<Any?>(null) }
    var searchQuery by rememberSaveable { mutableStateOf("") }

    LaunchedEffect(artistId) {
        viewModel.load(artistId)
    }

    Column(modifier = Modifier.fillMaxSize()) {
        val titleText = (uiState as? ArtistDetailUiState.Success)?.artist?.name ?: "Artist"
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
                (uiState as? ArtistDetailUiState.Success)?.artist?.let { artist ->
                    AddToPlaylistButton(
                        entityType = "artist",
                        entityKey = artist.id.toString(),
                        suggestedName = artist.name
                    )
                }
            }
        )

        when (val state = uiState) {
            is ArtistDetailUiState.Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)
            }
            is ArtistDetailUiState.Error -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(
                    text = state.message,
                    color = MaterialTheme.colorScheme.error,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(32.dp)
                )
            }
            is ArtistDetailUiState.Success -> ArtistDetailSuccess(
                artist = state.artist,
                baseUrl = viewModel.getBaseUrl(),
                query = searchQuery,
                favoriteAlbums = favoritesByType["album"].orEmpty(),
                onAlbumClick = onAlbumClick,
                onPlayAll = { viewModel.playAll(shuffle = false, onStarted = onPlayerClick) },
                onShuffleAll = { viewModel.playAll(shuffle = true, onStarted = onPlayerClick) },
                onArtistLongPress = { actionTarget = state.artist },
                onAlbumLongPress = { actionTarget = it }
            )
        }
    }

    when (val current = actionTarget) {
        is MusicArtistDetail -> {
            val key = current.id.toString()
            ItemActionSheet(
                title = current.name,
                subtitle = "${current.albums.size} albums",
                kind = ItemKind.Artist,
                isFavorited = key in favoritesByType["artist"].orEmpty(),
                onDismiss = { actionTarget = null },
                onToggleFavorite = { viewModel.toggleFavorite("artist", key) },
                onAddToPlaylist = { pickerTarget = current },
                onCreatePlaylist = { newPlaylistTarget = current }
            )
        }
        is AlbumSummary -> {
            val key = current.id.toString()
            ItemActionSheet(
                title = current.title,
                subtitle = current.artistName,
                kind = ItemKind.Album,
                isFavorited = key in favoritesByType["album"].orEmpty(),
                onDismiss = { actionTarget = null },
                onToggleFavorite = { viewModel.toggleFavorite("album", key) },
                onAddToPlaylist = { pickerTarget = current },
                onCreatePlaylist = { newPlaylistTarget = current }
            )
        }
        else -> {}
    }

    pickerTarget?.let { target ->
        PlaylistPickerSheet(
            playlists = playlists,
            onDismiss = { pickerTarget = null },
            onPick = { playlist ->
                val (kind, key) = artistEntityFor(target)
                viewModel.addEntityToPlaylist(kind, key, playlist.id)
            },
            onCreateNew = { newPlaylistTarget = target }
        )
    }

    newPlaylistTarget?.let { target ->
        val name = when (target) {
            is MusicArtistDetail -> target.name
            is AlbumSummary -> target.title
            else -> ""
        }
        NewPlaylistDialog(
            suggestedName = name,
            onDismiss = { newPlaylistTarget = null },
            onConfirm = { confirmedName ->
                val (kind, key) = artistEntityFor(target)
                viewModel.createPlaylistFromEntity(kind, key, confirmedName)
                newPlaylistTarget = null
            }
        )
    }
}

private fun artistEntityFor(target: Any?): Pair<String, String> = when (target) {
    is MusicArtistDetail -> "artist" to target.id.toString()
    is AlbumSummary -> "album" to target.id.toString()
    else -> "artist" to ""
}

@Composable
private fun ArtistDetailSuccess(
    artist: MusicArtistDetail,
    baseUrl: String,
    query: String,
    favoriteAlbums: Set<String>,
    onAlbumClick: (Int) -> Unit,
    onPlayAll: () -> Unit,
    onShuffleAll: () -> Unit,
    onArtistLongPress: () -> Unit,
    onAlbumLongPress: (AlbumSummary) -> Unit
) {
    Column(modifier = Modifier.fillMaxSize()) {
        if (artist.albums.isNotEmpty()) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp, vertical = 12.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Button(
                    onClick = onPlayAll,
                    modifier = Modifier.weight(1f)
                ) {
                    Icon(Icons.Default.PlayArrow, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text("Play all")
                }
                OutlinedButton(
                    onClick = onShuffleAll,
                    modifier = Modifier.weight(1f)
                ) {
                    Icon(Icons.Default.Shuffle, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text("Shuffle all")
                }
            }
            // A small affordance to favorite/add-to-playlist for the whole
            // artist — long-pressing the album grid only targets albums.
            Text(
                text = "Long-press artist for options",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier
                    .padding(horizontal = 16.dp, vertical = 4.dp)
                    .clickable(onClick = onArtistLongPress)
            )
        }
        AlbumsGrid(
            albums = artist.albums.filter { it.title.contains(query, ignoreCase = true) },
            baseUrl = baseUrl,
            favorites = favoriteAlbums,
            favoritesOnly = false,
            onClick = onAlbumClick,
            onLongClick = onAlbumLongPress
        )
    }
}
