package com.audiplex.app.ui.music

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.db.RecentlyPlayedDao
import com.audiplex.app.data.db.RecentlyPlayedTrackEntity
import com.audiplex.app.playback.PlaybackManager
import com.audiplex.app.playback.RecentlyPlayed
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import javax.inject.Inject

@HiltViewModel
class RecentlyPlayedViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val playbackManager: PlaybackManager,
    recentlyPlayedDao: RecentlyPlayedDao
) : ViewModel() {

    val rows: StateFlow<List<RecentlyPlayedTrackEntity>> =
        recentlyPlayedDao.observeRecent(100)
            .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())

    /**
     * Replay from the tapped row. The whole remembered list is loaded as the
     * queue (newest first) and playback jumps to the tapped index — so a tap
     * both answers "what was that track" and lets it keep going. Everything
     * needed to build the queue is on the rows themselves, so there is no API
     * round-trip (RecentlyPlayed.albumLookup).
     */
    fun play(startIndex: Int, onStarted: () -> Unit = {}) {
        val list = rows.value
        if (startIndex !in list.indices) return
        playbackManager.playTracks(
            tracks = RecentlyPlayed.toTracks(list),
            baseUrl = apiHolder.baseUrl,
            title = "Recently played",
            albumLookup = RecentlyPlayed.albumLookup(list)
        )
        if (startIndex > 0) playbackManager.seekToTrack(startIndex)
        onStarted()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RecentlyPlayedScreen(
    onBack: () -> Unit,
    onPlayerClick: () -> Unit,
    viewModel: RecentlyPlayedViewModel = hiltViewModel()
) {
    val rows by viewModel.rows.collectAsState()

    Column(modifier = Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text("Recently played") },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                }
            },
            colors = TopAppBarDefaults.topAppBarColors(
                containerColor = MaterialTheme.colorScheme.surface
            )
        )

        if (rows.isEmpty()) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(
                    "Nothing played yet — start a track and it'll show up here.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(32.dp)
                )
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                verticalArrangement = Arrangement.spacedBy(4.dp)
            ) {
                item {
                    Text(
                        text = "Last ${rows.size} tracks",
                        style = MaterialTheme.typography.titleMedium,
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp)
                    )
                }
                itemsIndexed(rows) { index, row ->
                    TrackRow(
                        index = index + 1,
                        track = RecentlyPlayed.toTrack(row),
                        onClick = { viewModel.play(startIndex = index, onStarted = onPlayerClick) }
                    )
                    if (index < rows.size - 1) {
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
