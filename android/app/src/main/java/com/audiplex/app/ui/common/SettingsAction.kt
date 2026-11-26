package com.audiplex.app.ui.common

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Tune  // #7044 sliders glyph: Pantheon uses the speaker for mute
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

/** #7044 (Todd 68920): every volume slider one tap away, so the Volumes icon rides beside every gear. */
val LocalOpenLevels = staticCompositionLocalOf<(() -> Unit)?> { null }  // #7044

@Composable
fun LevelsAction() {  // #7044
    val open = LocalOpenLevels.current ?: return  // #7044
    IconButton(onClick = open) {  // #7044
        Icon(Icons.Filled.Tune, contentDescription = "Volumes")  // #7044
    }  // #7044
}  // #7044

@Composable
fun SettingsAction() {
    LevelsAction()  // #7044
    val open = LocalOpenSettings.current ?: return
    IconButton(onClick = open) {
        Icon(Icons.Default.Settings, contentDescription = "Settings")
    }
}
