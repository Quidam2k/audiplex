package com.audiplex.app.ui.player

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.SkipNext
import androidx.compose.material3.ElevatedCard
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.audiplex.app.playback.MusicQueueState
import com.audiplex.app.playback.ORIGIN_DJ
import com.audiplex.app.playback.PlayerKind

internal data class NowPlayingCardModel(
    val queueLabel: String,
    val title: String,
    val artist: String?,
    val upNext: List<String>,
)

internal fun nowPlayingCardModel(
    kind: PlayerKind?,
    queue: MusicQueueState?,
): NowPlayingCardModel? {
    if (kind != PlayerKind.Music || queue == null || queue.items.isEmpty()) return null
    val current = queue.items.getOrNull(queue.currentIndex) ?: return null
    val queueTitle = if (queue.origin == ORIGIN_DJ) "DJ queue" else queue.title
    return NowPlayingCardModel(
        queueLabel = "$queueTitle · ${queue.currentIndex + 1}/${queue.items.size}",
        title = current.track.title,
        artist = current.track.artistName,
        upNext = queue.items.asSequence()
            .drop(queue.currentIndex + 1)
            .take(3)
            .map { it.track.title }
            .toList(),
    )
}

/** #7528: Now playing card at the top of both home screens so the DJ queue is findable. */
@Composable
fun NowPlayingCard(
    viewModel: PlayerViewModel,
    onOpen: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val kind by viewModel.playerKind.collectAsState()
    val queue by viewModel.currentMusic.collectAsState()
    val isPlaying by viewModel.isPlaying.collectAsState()
    val model = nowPlayingCardModel(kind, queue) ?: return

    ElevatedCard(
        onClick = onOpen,
        modifier = modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = "Now playing",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.primary,
                )
                Text(
                    text = model.queueLabel,
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = model.title,
                        style = MaterialTheme.typography.titleMedium,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    model.artist?.let { artist ->
                        Text(
                            text = artist,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                }
                IconButton(onClick = { viewModel.togglePlayPause() }) {
                    Icon(
                        imageVector = if (isPlaying) Icons.Default.Pause else Icons.Default.PlayArrow,
                        contentDescription = if (isPlaying) "Pause" else "Resume",
                    )
                }
                IconButton(onClick = { viewModel.nextChapter() }) {
                    Icon(imageVector = Icons.Default.SkipNext, contentDescription = "Skip")
                }
            }
            if (model.upNext.isNotEmpty()) {
                Text(
                    text = "Up next",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                model.upNext.forEach { title ->
                    Text(
                        text = "• $title",
                        style = MaterialTheme.typography.bodySmall,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
            TextButton(onClick = onOpen, modifier = Modifier.align(Alignment.End)) {
                Text(
                    text = "Open queue",
                    style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.primary,
                )
            }
        }
    }
}
