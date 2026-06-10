package com.audiplex.app.ui.music

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
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.audiplex.app.ui.common.AddToPlaylistButton
import com.audiplex.app.ui.common.SearchTopBar

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AlbumsScreen(
    genre: String?,
    artistId: Int?,
    title: String,
    onBack: () -> Unit,
    onAlbumClick: (Int) -> Unit,
    onPlayerClick: () -> Unit,
    viewModel: AlbumsViewModel = hiltViewModel()
) {
    val uiState by viewModel.uiState.collectAsState()
    val favoritesByType by viewModel.favoritesByType.collectAsState()
    var searchQuery by rememberSaveable { mutableStateOf("") }

    LaunchedEffect(genre, artistId) {
        viewModel.load(genre = genre, artistId = artistId, title = title)
    }

    Column(modifier = Modifier.fillMaxSize()) {
        SearchTopBar(
            title = title,
            searchQuery = searchQuery,
            onSearchQueryChange = { searchQuery = it },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                }
            },
            actions = {
                when {
                    genre != null -> AddToPlaylistButton(
                        entityType = "genre",
                        entityKey = genre,
                        suggestedName = title
                    )
                    artistId != null -> AddToPlaylistButton(
                        entityType = "artist",
                        entityKey = artistId.toString(),
                        suggestedName = title
                    )
                }
            }
        )

        when (val state = uiState) {
            is AlbumsUiState.Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)
            }
            is AlbumsUiState.Error -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(
                    text = state.message,
                    color = MaterialTheme.colorScheme.error,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(32.dp)
                )
            }
            is AlbumsUiState.Success -> Column(modifier = Modifier.fillMaxSize()) {
                // Play/Shuffle All only makes sense when we're filtering by
                // a single genre — for the artist case the ArtistDetailScreen
                // already shows the same row before its album grid.
                if (state.genre != null && state.albums.isNotEmpty()) {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 16.dp, vertical = 12.dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Button(
                            onClick = {
                                viewModel.playGenre(state.genre, shuffle = false, onStarted = onPlayerClick)
                            },
                            modifier = Modifier.weight(1f)
                        ) {
                            Icon(Icons.Default.PlayArrow, contentDescription = null)
                            Spacer(Modifier.width(8.dp))
                            Text("Play all")
                        }
                        OutlinedButton(
                            onClick = {
                                viewModel.playGenre(state.genre, shuffle = true, onStarted = onPlayerClick)
                            },
                            modifier = Modifier.weight(1f)
                        ) {
                            Icon(Icons.Default.Shuffle, contentDescription = null)
                            Spacer(Modifier.width(8.dp))
                            Text("Shuffle all")
                        }
                    }
                }
                AlbumsGrid(
                    albums = state.albums.filter { it.title.contains(searchQuery, ignoreCase = true) },
                    baseUrl = viewModel.getBaseUrl(),
                    favorites = favoritesByType["album"].orEmpty(),
                    favoritesOnly = false,
                    onClick = onAlbumClick,
                    onLongClick = null
                )
            }
        }
    }
}
