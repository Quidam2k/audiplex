package com.audiplex.app.ui.common

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.staticCompositionLocalOf

/**
 * #4221: Settings is one tap away on every screen, the Now Playing view included
 * (Todd had to back out two levels to find the gear). RootScaffold provides the
 * navigation; a screen just drops SettingsAction() into its top bar's actions.
 */
val LocalOpenSettings = staticCompositionLocalOf<(() -> Unit)?> { null }

@Composable
fun SettingsAction() {
    val open = LocalOpenSettings.current ?: return
    IconButton(onClick = open) {
        Icon(Icons.Default.Settings, contentDescription = "Settings")
    }
}
