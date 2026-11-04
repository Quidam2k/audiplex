package com.audiplex.app.ui.common

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Home
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.staticCompositionLocalOf

/**
 * #5396: a visible Home button on every screen. One tap clears the back stack and lands on
 * Audiobooks > Books, always the same place, so he never has to press back several times.
 * RootScaffold provides the navigation; a screen drops HomeAction() into its top bar's actions.
 */
val LocalGoHome = staticCompositionLocalOf<(() -> Unit)?> { null }

/** #5396: counts Home taps, so the Library screen knows to switch back to its Books tab. */
val LocalHomeTick = compositionLocalOf { 0 }

@Composable
fun HomeAction() {
    val goHome = LocalGoHome.current ?: return
    IconButton(onClick = goHome) {
        Icon(Icons.Default.Home, contentDescription = "Home")
    }
}
