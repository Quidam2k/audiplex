package com.audiplex.app.ui.music

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
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
import androidx.compose.material.icons.filled.Shuffle
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.api.SkipSuspectSchema
import com.audiplex.app.data.api.TrackSchema
import com.audiplex.app.playback.PlaybackManager
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class MostPlayedViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val playbackManager: PlaybackManager
) : ViewModel() {
    private val _tracks = MutableStateFlow<List<TrackSchema>>(emptyList())
    val tracks: StateFlow<List<TrackSchema>> = _tracks
    private val _loading = MutableStateFlow(true)
    val loading: StateFlow<Boolean> = _loading

    fun load(limit: Int = 50) {
        viewModelScope.launch {
            _loading.value = true
            try {
                _tracks.value = apiHolder.api?.getMostPlayed(limit).orEmpty()
            } catch (_: Exception) {
                _tracks.value = emptyList()
            } finally {
                _loading.value = false
            }
        }
    }

    fun play(shuffle: Boolean, startIndex: Int = 0, onStarted: () -> Unit = {}) {
        val list = _tracks.value
        if (list.isEmpty()) return
        viewModelScope.launch {
            // Build album lookup from /api/music/albums (one call instead of
            // per-track album fetches).
            val albumLookup = try {
                apiHolder.api?.getAlbums()?.associate {
                    it.id to (it.title to it.hasCover)
                }.orEmpty()
            } catch (_: Exception) {
                emptyMap()
            }
            playbackManager.playTracks(
                tracks = list,
                baseUrl = apiHolder.baseUrl,
                title = "Most Played",
                albumLookup = albumLookup,
                shuffle = shuffle
            )
            if (startIndex > 0) {
                playbackManager.seekToTrack(startIndex)
            }
            onStarted()
        }
    }

    fun getBaseUrl(): String = apiHolder.baseUrl
}

@HiltViewModel
class LikelySkipsViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val playbackManager: PlaybackManager
) : ViewModel() {
    private val _items = MutableStateFlow<List<SkipSuspectSchema>>(emptyList())
    val items: StateFlow<List<SkipSuspectSchema>> = _items
    private val _loading = MutableStateFlow(true)
    val loading: StateFlow<Boolean> = _loading

    fun load() {
        viewModelScope.launch {
            _loading.value = true
            try {
                _items.value = apiHolder.api?.getLikelySkips().orEmpty()
            } catch (_: Exception) {
                _items.value = emptyList()
            } finally {
                _loading.value = false
            }
        }
    }

    fun playTrack(track: TrackSchema, onStarted: () -> Unit = {}) {
        viewModelScope.launch {
            val albumLookup = try {
                apiHolder.api?.getAlbums()?.associate {
                    it.id to (it.title to it.hasCover)
                }.orEmpty()
            } catch (_: Exception) {
                emptyMap()
            }
            playbackManager.playTracks(
                tracks = listOf(track),
                baseUrl = apiHolder.baseUrl,
                title = "Preview",
                albumLookup = albumLookup
            )
            onStarted()
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MostPlayedScreen(
    onBack: () -> Unit,
    onPlayerClick: () -> Unit,
    viewModel: MostPlayedViewModel = hiltViewModel()
) {
    val tracks by viewModel.tracks.collectAsState()
    val loading by viewModel.loading.collectAsState()
    LaunchedEffect(Unit) { viewModel.load() }

    Column(modifier = Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text("Most Played") },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                }
            },
            colors = TopAppBarDefaults.topAppBarColors(
                containerColor = MaterialTheme.colorScheme.surface
            )
        )

        when {
            loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)
            }
            tracks.isEmpty() -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(
                    "No play history yet. Listen to a few tracks and they'll show up here.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(32.dp)
                )
            }
            else -> LazyColumn(
                modifier = Modifier.fillMaxSize(),
                verticalArrangement = Arrangement.spacedBy(4.dp)
            ) {
                item {
                    Text(
                        text = "Top ${tracks.size} tracks",
                        style = MaterialTheme.typography.titleMedium,
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp)
                    )
                }
                item {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 16.dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Button(
                            onClick = { viewModel.play(shuffle = false, onStarted = onPlayerClick) },
                            modifier = Modifier.weight(1f)
                        ) {
                            Icon(Icons.Default.PlayArrow, contentDescription = null)
                            Spacer(Modifier.width(8.dp))
                            Text("Play all")
                        }
                        OutlinedButton(
                            onClick = { viewModel.play(shuffle = true, onStarted = onPlayerClick) },
                            modifier = Modifier.weight(1f)
                        ) {
                            Icon(Icons.Default.Shuffle, contentDescription = null)
                            Spacer(Modifier.width(8.dp))
                            Text("Shuffle")
                        }
                    }
                }

                itemsIndexed(tracks) { index, track ->
                    TrackRow(
                        index = index + 1,
                        track = track,
                        onClick = {
                            viewModel.play(shuffle = false, startIndex = index, onStarted = onPlayerClick)
                        }
                    )
                    if (index < tracks.size - 1) {
                        HorizontalDivider(
                            modifier = Modifier.padding(horizontal = 16.dp),
                            color = MaterialTheme.colorScheme.surfaceVariant
                        )
                    }
                }
                item { Spacer(Modifier.height(80.dp)) }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LikelySkipsScreen(
    onBack: () -> Unit,
    onPlayerClick: () -> Unit,
    viewModel: LikelySkipsViewModel = hiltViewModel()
) {
    val items by viewModel.items.collectAsState()
    val loading by viewModel.loading.collectAsState()
    LaunchedEffect(Unit) { viewModel.load() }

    Column(modifier = Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text("Skip suspects") },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                }
            },
            colors = TopAppBarDefaults.topAppBarColors(
                containerColor = MaterialTheme.colorScheme.surface
            )
        )

        when {
            loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)
            }
            items.isEmpty() -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(
                    "Nothing stands out — no tracks have been skipped early enough to flag yet.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(32.dp)
                )
            }
            else -> LazyColumn(modifier = Modifier.fillMaxSize()) {
                item {
                    Text(
                        text = "${items.size} tracks frequently abandoned in the first 10 seconds",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp)
                    )
                }
                itemsIndexed(items) { _, suspect ->
                    SuspectRow(
                        suspect = suspect,
                        onClick = { viewModel.playTrack(suspect.track, onStarted = onPlayerClick) }
                    )
                    HorizontalDivider(
                        modifier = Modifier.padding(horizontal = 16.dp),
                        color = MaterialTheme.colorScheme.surfaceVariant
                    )
                }
                item { Spacer(Modifier.height(80.dp)) }
            }
        }
    }
}

@Composable
private fun SuspectRow(suspect: SkipSuspectSchema, onClick: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = suspect.track.title,
                style = MaterialTheme.typography.bodyMedium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            Text(
                text = listOfNotNull(
                    suspect.track.artistName,
                    "skipped ${suspect.earlySkipCount}× of ${suspect.totalStarts} starts"
                ).joinToString(" • "),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
        }
        IconButton(onClick = onClick) {
            Icon(
                Icons.Default.PlayArrow,
                contentDescription = "Preview",
                tint = MaterialTheme.colorScheme.primary
            )
        }
    }
}
