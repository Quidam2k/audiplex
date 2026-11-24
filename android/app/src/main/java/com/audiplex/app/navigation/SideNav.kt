package com.audiplex.app.navigation

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Book
import androidx.compose.material.icons.filled.MusicNote
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBarDefaults
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp

/** Defines the two navigation sides for #6781. */
internal enum class Side(val root: String, val label: String, val icon: ImageVector) {
    AUDIOBOOKS(Routes.LIBRARY, "Audiobooks", Icons.Default.Book),
    MUSIC(Routes.MUSIC_HOME, "Music", Icons.Default.MusicNote),
}

/**
 * #6781: Music is the side whenever its root is on the back stack. Music home is reached only
 * through the bar, so it always sits right on Library, and leaving Music pops it. Player,
 * Settings and Downloads therefore read as the side they were opened from.
 */
internal fun sideOf(musicOnStack: Boolean): Side = if (musicOnStack) Side.MUSIC else Side.AUDIOBOOKS

/**
 * #6781: the bar is on every screen but Login, and only once Library is on the stack (not on a
 * Player opened from Login after a logout, where a tap could restore the old session's screens).
 */
internal fun showsSideBar(route: String?, libraryOnStack: Boolean): Boolean =
    route != null && route != Routes.LOGIN && libraryOnStack

/** Lists the possible results of tapping a side for #6781. */
internal enum class SideTap {
    NOTHING,
    POP_TO_ROOT,
    SWITCH,
}

/** Resolves a tap against the current destination and side for #6781. */
internal fun sideTap(currentRoute: String?, currentSide: Side, tapped: Side): SideTap = when {
    currentRoute == tapped.root -> SideTap.NOTHING
    tapped == currentSide -> SideTap.POP_TO_ROOT
    else -> SideTap.SWITCH
}

/** Displays the compact bottom navigation bar for #6781. */
@Composable
internal fun SideBar(selected: Side, onTap: (Side) -> Unit, modifier: Modifier = Modifier) {
    val colors = MaterialTheme.colorScheme

    Surface(
        modifier = modifier.fillMaxWidth(),
        color = colors.surfaceContainer,
        tonalElevation = 3.dp,
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .windowInsetsPadding(NavigationBarDefaults.windowInsets)
                .height(52.dp)
                .selectableGroup(),
        ) {
            for (side in Side.entries) {
                val isSelected = side == selected
                val background = if (isSelected) colors.secondaryContainer else Color.Transparent
                val contentColor = if (isSelected) colors.onSecondaryContainer else colors.onSurfaceVariant

                Box(
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxHeight()
                        .selectable(
                            selected = isSelected,
                            role = Role.Tab,
                            onClick = { onTap(side) },
                        ),
                    contentAlignment = Alignment.Center,
                ) {
                    Row(
                        modifier = Modifier
                            .background(background, RoundedCornerShape(50))
                            .padding(horizontal = 16.dp, vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Icon(
                            imageVector = side.icon,
                            contentDescription = null,
                            modifier = Modifier.size(20.dp),
                            tint = contentColor,
                        )
                        Spacer(Modifier.width(6.dp))
                        Text(
                            text = side.label,
                            color = contentColor,
                            style = MaterialTheme.typography.labelLarge,
                            maxLines = 1,
                        )
                    }
                }
            }
        }
    }
}
