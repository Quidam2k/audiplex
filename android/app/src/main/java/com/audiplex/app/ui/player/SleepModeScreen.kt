package com.audiplex.app.ui.player

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextAlign
import com.audiplex.app.playback.BedState

/**
 * #5876: the full-screen sleep view. Black so the room stays dark, one dim line, and a tap anywhere
 * calls [onWake] (the sleep sound fades out in about a second and the app returns to normal).
 * It sits over the whole app; the media notification and lock-screen controls are untouched.
 */
@Composable
fun SleepModeScreen(bedState: BedState, onWake: () -> Unit) {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black)
            .clickable(
                interactionSource = remember { MutableInteractionSource() },
                indication = null,
                onClick = onWake,
            ),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = if (bedState == BedState.Stopped) "Sleep sound stopped. Tap to leave sleep mode."
            else "Sleep mode. Tap anywhere to wake.",
            style = MaterialTheme.typography.bodyMedium,
            color = Color(0x55FFFFFF),
            textAlign = TextAlign.Center,
        )
    }
}
