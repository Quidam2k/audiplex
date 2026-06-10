package com.audiplex.app.ui.player

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Book
import androidx.compose.material.icons.filled.Forward30
import androidx.compose.material.icons.filled.MusicNote
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Replay10
import androidx.compose.material.icons.filled.SkipNext
import androidx.compose.material.icons.filled.SkipPrevious
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import coil3.compose.AsyncImage
import com.audiplex.app.data.api.AudiplexApi
import com.audiplex.app.playback.PlayerKind

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PlayerScreen(
    onBack: () -> Unit,
    viewModel: PlayerViewModel = hiltViewModel()
) {
    val kind by viewModel.playerKind.collectAsState()
    val book by viewModel.currentBook.collectAsState()
    val music by viewModel.currentMusic.collectAsState()
    val isPlaying by viewModel.isPlaying.collectAsState()
    val positionMs by viewModel.positionMs.collectAsState()
    val durationMs by viewModel.durationMs.collectAsState()
    val chapterIndex by viewModel.currentChapterIndex.collectAsState()

    Column(
        modifier = Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        TopAppBar(
            title = { Text("Now Playing") },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                }
            },
            colors = TopAppBarDefaults.topAppBarColors(
                containerColor = MaterialTheme.colorScheme.surface
            )
        )

        if (kind == null) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text("Nothing playing", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            return
        }

        Spacer(Modifier.height(16.dp))

        // Cover + title block
        when (kind) {
            PlayerKind.Audiobook -> {
                val currentBook = book ?: return
                CoverArt(
                    coverUrl = if (currentBook.hasCover)
                        AudiplexApi.coverUrl(viewModel.getBaseUrl(), currentBook.id) else null,
                    contentDescription = currentBook.title,
                    fallbackIcon = Icons.Default.Book
                )
                Spacer(Modifier.height(24.dp))
                TitleBlock(
                    title = currentBook.title,
                    subtitle = currentBook.author,
                    detail = currentBook.chapters.getOrNull(chapterIndex)?.title
                )
            }
            PlayerKind.Music -> {
                val currentMusic = music ?: return
                val track = currentMusic.items.getOrNull(currentMusic.currentIndex)
                val albumId = track?.albumId
                val coverUrl = if (track?.albumHasCover == true && albumId != null)
                    AudiplexApi.musicCoverUrl(viewModel.getBaseUrl(), albumId) else null
                CoverArt(
                    coverUrl = coverUrl,
                    contentDescription = track?.track?.title ?: "",
                    fallbackIcon = Icons.Default.MusicNote
                )
                Spacer(Modifier.height(24.dp))
                TitleBlock(
                    title = track?.track?.title ?: "",
                    subtitle = track?.track?.artistName,
                    detail = track?.albumTitle
                )
            }
            null -> Unit
        }

        Spacer(Modifier.weight(1f))

        // Seek bar — for music, scoped to current track; for audiobook, scoped to whole book
        val effectiveDuration = when (kind) {
            PlayerKind.Music -> durationMs.coerceAtLeast(1L)
            PlayerKind.Audiobook -> {
                val b = book
                if (durationMs > 0) durationMs
                else (b?.durationSeconds?.let { (it * 1000).toLong() } ?: 1L)
            }
            null -> 1L
        }
        var isSeeking by remember { mutableStateOf(false) }
        var seekPosition by remember { mutableFloatStateOf(0f) }

        Column(modifier = Modifier.padding(horizontal = 24.dp)) {
            Slider(
                value = if (isSeeking) seekPosition else positionMs.toFloat(),
                onValueChange = {
                    isSeeking = true
                    seekPosition = it
                },
                onValueChangeFinished = {
                    viewModel.seekTo(seekPosition.toLong())
                    isSeeking = false
                },
                valueRange = 0f..effectiveDuration.toFloat().coerceAtLeast(1f),
                colors = SliderDefaults.colors(
                    thumbColor = MaterialTheme.colorScheme.primary,
                    activeTrackColor = MaterialTheme.colorScheme.primary
                )
            )

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = viewModel.formatTime(if (isSeeking) seekPosition.toLong() else positionMs),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Text(
                    text = viewModel.formatTime(effectiveDuration),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }

        Spacer(Modifier.height(16.dp))

        // Controls
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.Center,
            verticalAlignment = Alignment.CenterVertically
        ) {
            IconButton(onClick = { viewModel.previousChapter() }) {
                Icon(
                    Icons.Default.SkipPrevious,
                    contentDescription = if (kind == PlayerKind.Music) "Previous track" else "Previous chapter",
                    modifier = Modifier.size(32.dp)
                )
            }

            Spacer(Modifier.width(8.dp))

            // Skip controls only for audiobook
            if (kind == PlayerKind.Audiobook) {
                IconButton(onClick = { viewModel.skipBack() }) {
                    Icon(
                        Icons.Default.Replay10,
                        contentDescription = "Skip back 30s",
                        modifier = Modifier.size(36.dp)
                    )
                }
                Spacer(Modifier.width(16.dp))
            }

            FilledIconButton(
                onClick = { viewModel.togglePlayPause() },
                modifier = Modifier.size(64.dp),
                shape = CircleShape,
                colors = IconButtonDefaults.filledIconButtonColors(
                    containerColor = MaterialTheme.colorScheme.primary
                )
            ) {
                Icon(
                    if (isPlaying) Icons.Default.Pause else Icons.Default.PlayArrow,
                    contentDescription = if (isPlaying) "Pause" else "Play",
                    modifier = Modifier.size(36.dp)
                )
            }

            if (kind == PlayerKind.Audiobook) {
                Spacer(Modifier.width(16.dp))
                IconButton(onClick = { viewModel.skipForward() }) {
                    Icon(
                        Icons.Default.Forward30,
                        contentDescription = "Skip forward 30s",
                        modifier = Modifier.size(36.dp)
                    )
                }
            }

            Spacer(Modifier.width(8.dp))

            IconButton(onClick = { viewModel.nextChapter() }) {
                Icon(
                    Icons.Default.SkipNext,
                    contentDescription = if (kind == PlayerKind.Music) "Next track" else "Next chapter",
                    modifier = Modifier.size(32.dp)
                )
            }
        }

        Spacer(Modifier.height(48.dp))
    }
}

@Composable
private fun CoverArt(coverUrl: String?, contentDescription: String, fallbackIcon: ImageVector) {
    if (coverUrl != null) {
        AsyncImage(
            model = coverUrl,
            contentDescription = contentDescription,
            contentScale = ContentScale.Crop,
            modifier = Modifier
                .fillMaxWidth(0.7f)
                .aspectRatio(1f)
                .clip(RoundedCornerShape(16.dp))
        )
    } else {
        Box(
            modifier = Modifier
                .fillMaxWidth(0.7f)
                .aspectRatio(1f)
                .clip(RoundedCornerShape(16.dp)),
            contentAlignment = Alignment.Center
        ) {
            Icon(
                fallbackIcon,
                contentDescription = null,
                modifier = Modifier.size(96.dp),
                tint = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

@Composable
private fun TitleBlock(title: String, subtitle: String?, detail: String?) {
    Text(
        text = title,
        style = MaterialTheme.typography.titleLarge,
        textAlign = TextAlign.Center,
        maxLines = 2,
        overflow = TextOverflow.Ellipsis,
        modifier = Modifier.padding(horizontal = 32.dp)
    )
    subtitle?.let {
        Text(
            text = it,
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.primary,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(horizontal = 32.dp)
        )
    }
    detail?.let {
        Spacer(Modifier.height(4.dp))
        Text(
            text = it,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(horizontal = 32.dp)
        )
    }
}
