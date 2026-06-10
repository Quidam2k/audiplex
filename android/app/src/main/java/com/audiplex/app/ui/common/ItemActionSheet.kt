package com.audiplex.app.ui.common

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.PlaylistAdd
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.BookmarkBorder
import androidx.compose.material.icons.filled.Star
import androidx.compose.material.icons.filled.StarBorder
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp

enum class ItemKind { Track, Album, Artist, Genre, Playlist, Book }

/**
 * Bottom sheet for actions on any music or library entity.
 *
 * Each callback is wired as a row that, when tapped, dismisses the sheet
 * and forwards the action. The caller decides which actions are
 * applicable for a given kind — for music tracks/albums/artists/genres
 * you get Favorite + playlist actions, for books you get Favorite +
 * to-read.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ItemActionSheet(
    title: String,
    subtitle: String?,
    kind: ItemKind,
    isFavorited: Boolean,
    isInToRead: Boolean = false,
    onDismiss: () -> Unit,
    onToggleFavorite: () -> Unit,
    onAddToPlaylist: (() -> Unit)? = null,
    onCreatePlaylist: (() -> Unit)? = null,
    onToggleToRead: (() -> Unit)? = null
) {
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState
    ) {
        Column(modifier = Modifier.padding(bottom = 24.dp)) {
            Column(modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp)) {
                Text(
                    text = title,
                    style = MaterialTheme.typography.titleMedium
                )
                if (!subtitle.isNullOrBlank()) {
                    Text(
                        text = subtitle,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
            SheetRow(
                icon = if (isFavorited) Icons.Default.Star else Icons.Default.StarBorder,
                label = if (isFavorited) "Remove from favorites" else "Favorite",
                onClick = {
                    onToggleFavorite()
                    onDismiss()
                }
            )
            if (kind == ItemKind.Book && onToggleToRead != null) {
                SheetRow(
                    icon = if (isInToRead) Icons.Default.Bookmark else Icons.Default.BookmarkBorder,
                    label = if (isInToRead) "Remove from Want to read" else "Add to Want to read",
                    onClick = {
                        onToggleToRead()
                        onDismiss()
                    }
                )
            }
            if (onAddToPlaylist != null) {
                SheetRow(
                    icon = Icons.AutoMirrored.Filled.PlaylistAdd,
                    label = "Add to playlist…",
                    onClick = {
                        onAddToPlaylist()
                        onDismiss()
                    }
                )
            }
            if (onCreatePlaylist != null) {
                SheetRow(
                    icon = Icons.Default.Add,
                    label = "New playlist from this…",
                    onClick = {
                        onCreatePlaylist()
                        onDismiss()
                    }
                )
            }
        }
    }
}

@Composable
private fun SheetRow(icon: ImageVector, label: String, onClick: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(horizontal = 24.dp, vertical = 16.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.Start
    ) {
        Icon(icon, contentDescription = null, tint = MaterialTheme.colorScheme.primary)
        Spacer(Modifier.width(16.dp))
        Text(text = label, style = MaterialTheme.typography.bodyLarge)
    }
}
