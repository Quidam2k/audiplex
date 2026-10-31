package com.audiplex.app.ui.player

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.audiplex.app.playback.MixLevels

/** #5282 (Todd 53042): a level slider with a mark at full; right of the mark is past the sound's own level. */
@Composable
fun PastFullSlider(position: Float, onPosition: (Float) -> Unit, onFinished: () -> Unit = {}) {
    Column {
        Slider(
            value = position,
            valueRange = 0f..1f,
            onValueChange = onPosition,
            onValueChangeFinished = onFinished,
        )
        Row(Modifier.fillMaxWidth().padding(horizontal = 10.dp)) {  // the thumb's inset at each end of the track
            Spacer(Modifier.weight(MixLevels.FULL_POSITION))
            Box(Modifier.width(2.dp).height(8.dp).background(MaterialTheme.colorScheme.error))
            Spacer(Modifier.weight(1f - MixLevels.FULL_POSITION))
        }
    }
}
