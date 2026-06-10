package com.audiplex.app.ui.music

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Shuffle
import androidx.compose.material.icons.filled.Star
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.audiplex.app.data.api.AlbumDetail
import com.audiplex.app.data.api.TrackSchema
import com.audiplex.app.ui.common.AddToPlaylistButton
import com.audiplex.app.ui.common.SearchTopBar
import com.audiplex.app.ui.common.ItemActionSheet
import com.audiplex.app.ui.common.ItemKind
import com.audiplex.app.ui.common.NewPlaylistDialog
import com.audiplex.app.ui.common.PlaylistPickerSheet

private data class TrackTarget(
    val trackId: Int,
    val title: String,
    val subtitle: String?
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AlbumDetailScreen(
    albumId: Int,
    onBack: () -> Unit,
    onPlayerClick: () -> Unit,
    viewModel: AlbumDetailViewModel = hiltViewModel()
) {
    val uiState by viewModel.uiState.collectAsState()
    val favoritesByType by viewModel.favoritesByType.collectAsState()
    val playlists by viewModel.playlists.collectAsState()

    var actionTarget by remember { mutableStateOf<Any?>(null) }  // TrackTarget or AlbumDetail
    var pickerTarget by remember { mutableStateOf<Any?>(null) }
    var newPlaylistTarget by remember { mutableStateOf<Any?>(null) }
    var searchQuery by rememberSaveable { mutableStateOf("") }

    LaunchedEffect(albumId) {
        viewModel.load(albumId)
    }

    Column(modifier = Modifier.fillMaxSize()) {
        SearchTopBar(
            title = "Album",
            searchQuery = searchQuery,
            onSearchQueryChange = { searchQuery = it },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                }
            },
            actions = {
                (uiState as? AlbumDetailUiState.Success)?.album?.let { album ->
                    AddToPlaylistButton(
                        entityType = "album",
                        entityKey = album.id.toString(),
                        suggestedName = album.title
                    )
                }
            }
        )

        when (val state = uiState) {
            is AlbumDetailUiState.Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)
            }
            is AlbumDetailUiState.Error -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(
                    text = state.message,
                    color = MaterialTheme.colorScheme.error,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(32.dp)
                )
            }
            is AlbumDetailUiState.Success -> AlbumDetailContent(
                album = state.album,
                baseUrl = viewModel.getBaseUrl(),
                query = searchQuery,
                favoriteAlbums = favoritesByType["album"].orEmpty(),
                favoriteTracks = favoritesByType["track"].orEmpty(),
                onPlayAlbum = {
                    viewModel.playAlbum(state.album, 0)
                    onPlayerClick()
                },
                onShuffleAlbum = {
                    viewModel.shuffleAlbum(state.album)
                    onPlayerClick()
                },
                onTrackClick = { index ->
                    viewModel.playAlbum(state.album, index)
                    onPlayerClick()
                },
                onAlbumLongPress = { actionTarget = state.album },
                onTrackLongPress = { idx, track ->
                    actionTarget = TrackTarget(
                        trackId = track.id,
                        title = track.title,
                        subtitle = track.artistName
                    )
                }
            )
        }
    }

    // Action sheet
    val current = actionTarget
    if (current is TrackTarget) {
        val key = current.trackId.toString()
        ItemActionSheet(
            title = current.title,
            subtitle = current.subtitle,
            kind = ItemKind.Track,
            isFavorited = key in favoritesByType["track"].orEmpty(),
            onDismiss = { actionTarget = null },
            onToggleFavorite = { viewModel.toggleFavorite("track", key) },
            onAddToPlaylist = { pickerTarget = current },
            onCreatePlaylist = { newPlaylistTarget = current }
        )
    } else if (current is AlbumDetail) {
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

    pickerTarget?.let { target ->
        PlaylistPickerSheet(
            playlists = playlists,
            onDismiss = { pickerTarget = null },
            onPick = { playlist ->
                val (kind, key) = entityFor(target)
                viewModel.addEntityToPlaylist(kind, key, playlist.id)
            },
            onCreateNew = { newPlaylistTarget = target }
        )
    }

    newPlaylistTarget?.let { target ->
        val name = when (target) {
            is TrackTarget -> target.title
            is AlbumDetail -> target.title
            else -> ""
        }
        NewPlaylistDialog(
            suggestedName = name,
            onDismiss = { newPlaylistTarget = null },
            onConfirm = { confirmedName ->
                val (kind, key) = entityFor(target)
                viewModel.createPlaylistFromEntity(kind, key, confirmedName)
                newPlaylistTarget = null
            }
        )
    }
}

private fun entityFor(target: Any?): Pair<String, String> = when (target) {
    is TrackTarget -> "track" to target.trackId.toString()
    is AlbumDetail -> "album" to target.id.toString()
    else -> "track" to ""
}

@Composable
private fun AlbumDetailContent(
    album: AlbumDetail,
    baseUrl: String,
    query: String,
    favoriteAlbums: Set<String>,
    favoriteTracks: Set<String>,
    onPlayAlbum: () -> Unit,
    onShuffleAlbum: () -> Unit,
    onTrackClick: (Int) -> Unit,
    onAlbumLongPress: () -> Unit,
    onTrackLongPress: (Int, TrackSchema) -> Unit
) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        item {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(16.dp)
            ) {
                AlbumCoverImage(
                    albumId = album.id,
                    hasCover = album.hasCover,
                    title = album.title,
                    baseUrl = baseUrl,
                    modifier = Modifier
                        .size(160.dp)
                        .clip(RoundedCornerShape(8.dp))
                )
                Spacer(Modifier.width(16.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        if (album.id.toString() in favoriteAlbums) {
                            Icon(
                                Icons.Default.Star,
                                contentDescription = "Favorite",
                                tint = MaterialTheme.colorScheme.primary,
                                modifier = Modifier.size(20.dp)
                            )
                            Spacer(Modifier.size(6.dp))
                        }
                        Text(album.title, style = MaterialTheme.typography.titleLarge)
                    }
                    Spacer(Modifier.height(4.dp))
                    album.artistName?.let {
                        Text(
                            it,
                            style = MaterialTheme.typography.bodyLarge,
                            color = MaterialTheme.colorScheme.primary
                        )
                    }
                    val meta = listOfNotNull(
                        album.genre,
                        album.year?.toString(),
                        "${album.trackCount} tracks"
                    ).joinToString(" • ")
                    if (meta.isNotEmpty()) {
                        Text(
                            meta,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    Text(
                        text = formatDuration(album.durationSeconds),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(Modifier.height(6.dp))
                    Text(
                        text = "Long-press for options",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier
                            .clickable(onClick = onAlbumLongPress)
                    )
                }
            }
        }

        item {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Button(
                    onClick = onPlayAlbum,
                    modifier = Modifier.weight(1f)
                ) {
                    Icon(Icons.Default.PlayArrow, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text("Play album")
                }
                OutlinedButton(
                    onClick = onShuffleAlbum,
                    modifier = Modifier.weight(1f)
                ) {
                    Icon(Icons.Default.Shuffle, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text("Shuffle")
                }
            }
        }

        if (album.tracks.isNotEmpty()) {
            // Filter by title while preserving each track's ORIGINAL index —
            // playback seeks by position into the full list.
            val visibleTracks = album.tracks.withIndex()
                .filter { it.value.title.contains(query, ignoreCase = true) }

            item {
                Text(
                    "Tracks (${album.tracks.size})",
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
                    onLongClick = { onTrackLongPress(index, track) }
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

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun TrackRow(
    index: Int,
    track: TrackSchema,
    isFavorite: Boolean = false,
    onClick: () -> Unit,
    onLongClick: (() -> Unit)? = null
) {
    val rowModifier = if (onLongClick != null) {
        Modifier.combinedClickable(onClick = onClick, onLongClick = onLongClick)
    } else {
        Modifier.clickable(onClick = onClick)
    }
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .then(rowModifier)
            .padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(
            text = index.toString().padStart(2, ' '),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.width(28.dp)
        )
        Column(modifier = Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (isFavorite) {
                    Icon(
                        Icons.Default.Star,
                        contentDescription = "Favorite",
                        tint = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.size(14.dp)
                    )
                    Spacer(Modifier.width(4.dp))
                }
                Text(
                    text = track.title,
                    style = MaterialTheme.typography.bodyMedium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
            track.artistName?.let {
                Text(
                    text = it,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
        }
        Text(
            text = formatDuration(track.durationSeconds),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}

internal fun formatDuration(seconds: Double): String {
    val totalSeconds = seconds.toLong()
    val hours = totalSeconds / 3600
    val minutes = (totalSeconds % 3600) / 60
    val secs = totalSeconds % 60
    return if (hours > 0) {
        "%d:%02d:%02d".format(hours, minutes, secs)
    } else {
        "%d:%02d".format(minutes, secs)
    }
}
