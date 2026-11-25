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
import com.audiplex.app.ui.common.HomeAction  // #5396
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.ui.common.SettingsAction  // #4221
import com.audiplex.app.data.db.RecentlyPlayedDao
import com.audiplex.app.data.db.RecentlyPlayedTrackEntity
import android.os.SystemClock
import com.audiplex.app.playback.ClientLogReporter
import com.audiplex.app.playback.PlaybackManager
import com.audiplex.app.playback.RecentlyPlayed
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import javax.inject.Inject

@HiltViewModel
class RecentlyPlayedViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val playbackManager: PlaybackManager,
    private val clientLog: ClientLogReporter,  // #6999
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
        val stored = RecentlyPlayed.toTracks(list)
        playJob?.cancel()
        playJob = viewModelScope.launch {
            // #6999: one request for the current size + trim of every row, so the
            // cache key is fresh. Any failure or the cap plays the stored rows at once.
            val t0 = SystemClock.elapsedRealtime()
            var capped = true
            val fresh = try {
                withTimeoutOrNull(FRESH_TIMEOUT_MS) {
                    apiHolder.api?.getTracks(stored.joinToString(",") { it.id.toString() })
                        .also { capped = false }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                capped = false
                null
            }
            val ms = SystemClock.elapsedRealtime() - t0
            clientLog.report(
                level = "info",
                event = "recent_fresh",
                message = if (fresh != null) "fresh rows in ${ms}ms" else "stored rows after ${ms}ms",
                detail = mapOf("ms" to ms.toString(), "ok" to (fresh != null).toString(),
                    "capped" to capped.toString(), "count" to stored.size.toString()),
            )
            playbackManager.playTracks(
                tracks = if (fresh != null) RecentlyPlayed.withFresh(stored, fresh) else stored,
                baseUrl = apiHolder.baseUrl,
                title = "Recently played",
                albumLookup = RecentlyPlayed.albumLookup(list)
            )
            if (startIndex > 0) playbackManager.seekToTrack(startIndex)
            onStarted()
        }
    }

    private var playJob: Job? = null

    private companion object {
        const val FRESH_TIMEOUT_MS = 3_000L  // #6999
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
            actions = { HomeAction(); SettingsAction() },  // #4221
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
