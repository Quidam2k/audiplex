package com.audiplex.app.ui.player

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import com.audiplex.app.data.api.SleepBed
import com.audiplex.app.playback.BedState
import com.audiplex.app.playback.SleepFade

private const val TEST_MINUTES = 1

/** #3714 in-app sleep button */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SleepDialog(
    beds: List<SleepBed>,
    defaultBedId: Int?,
    bedsError: String?,
    minutesLeft: Int?,
    bedState: BedState,
    bedLevel: Float,
    onBedLevel: (Float) -> Unit,
    onRetryBeds: () -> Unit,
    onStart: (minutes: Int, bed: SleepBed?, fadeSeconds: Int) -> Unit,
    onExtend: () -> Unit,
    onCancel: () -> Unit,
    onStopBed: () -> Unit,
    onRestartBed: () -> Unit,
    onDismiss: () -> Unit,
) {
    val bedRunning = bedState == BedState.Playing || bedState == BedState.Recovering

    var minutes by rememberSaveable { mutableStateOf(30) }
    // Keyed on the default bed (last used, else first; #3953): the list loads
    // after the dialog opens, and the default must follow it instead of
    // sticking on Silence.
    var selectedBedId by rememberSaveable(defaultBedId) {
        mutableStateOf<Int?>(defaultBedId)
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Sleep") },
        text = {
            when {
                minutesLeft != null -> {
                    Text(
                        if (minutesLeft <= 0) {
                            "Fading out now"
                        } else {
                            "Fades out in $minutesLeft min"
                        }
                    )
                }

                bedRunning -> Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(
                        if (bedState == BedState.Recovering) "Sleep bed dropped out. Restarting it..."
                        else "Sleep bed is playing."
                    )
                    BedVolumeRow(bedLevel, onBedLevel)
                }

                // #4018: a bed that died says so, with a one-tap restart.
                bedState == BedState.Stopped -> Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Sleep bed stopped.", color = MaterialTheme.colorScheme.error)
                    BedVolumeRow(bedLevel, onBedLevel)
                }

                else -> {
                    Column(
                        modifier = Modifier.verticalScroll(rememberScrollState()),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        Text("Keep the book playing, then fade into:")

                        // #3953: never let a failed bed list pass as a choice of Silence.
                        if (bedsError != null) {
                            Text("Couldn't load sleep beds: $bedsError")
                            TextButton(onClick = onRetryBeds) {
                                Text("Retry")
                            }
                        }

                        FlowRow(
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalArrangement = Arrangement.spacedBy(4.dp),
                        ) {
                            listOf(15, 30, 45, 60, 90).forEach { duration ->
                                FilterChip(
                                    selected = minutes == duration,
                                    onClick = { minutes = duration },
                                    label = { Text("$duration min") },
                                )
                            }
                            // #3953: hear the whole handoff before bed (1 min + short fade).
                            FilterChip(
                                selected = minutes == TEST_MINUTES,
                                onClick = { minutes = TEST_MINUTES },
                                label = { Text("Test: 1 min") },
                            )
                        }

                        Column(Modifier.selectableGroup()) {
                            beds.forEach { bed ->
                                Row(
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .heightIn(min = 48.dp)
                                        .selectable(
                                            selected = selectedBedId == bed.id,
                                            onClick = { selectedBedId = bed.id },
                                            role = Role.RadioButton,
                                        )
                                        .padding(horizontal = 4.dp),
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                                ) {
                                    RadioButton(
                                        selected = selectedBedId == bed.id,
                                        onClick = null,
                                    )
                                    Text(bed.title)
                                }
                            }

                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .heightIn(min = 48.dp)
                                    .selectable(
                                        selected = selectedBedId == null,
                                        onClick = { selectedBedId = null },
                                        role = Role.RadioButton,
                                    )
                                    .padding(horizontal = 4.dp),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(8.dp),
                            ) {
                                RadioButton(
                                    selected = selectedBedId == null,
                                    onClick = null,
                                )
                                Text("Silence")
                            }
                        }

                        if (beds.any { it.id == selectedBedId }) BedVolumeRow(bedLevel, onBedLevel)
                    }
                }
            }
        },
        confirmButton = {
            when {
                minutesLeft != null -> {
                    TextButton(onClick = onExtend) {
                        Text("+15 min")
                    }
                }

                bedState == BedState.Stopped -> {
                    TextButton(onClick = onRestartBed) {
                        Text("Restart bed")
                    }
                }

                bedRunning -> {
                    TextButton(
                        onClick = {
                            onStopBed()
                            onDismiss()
                        }
                    ) {
                        Text("Stop bed")
                    }
                }

                else -> {
                    TextButton(
                        onClick = {
                            onStart(
                                minutes,
                                beds.firstOrNull { it.id == selectedBedId },
                                if (minutes == TEST_MINUTES) SleepFade.TEST_FADE_SECONDS else SleepFade.DEFAULT_FADE_SECONDS,
                            )
                            onDismiss()
                        }
                    ) {
                        Text(if (beds.none { it.id == selectedBedId }) "Start (silence)" else "Start")
                    }
                }
            }
        },
        dismissButton = {
            if (minutesLeft != null) {
                Row {
                    TextButton(
                        onClick = {
                            onCancel()
                            onDismiss()
                        }
                    ) {
                        Text("Cancel timer")
                    }
                    TextButton(onClick = onDismiss) {
                        Text("Close")
                    }
                }
            } else if (bedState == BedState.Stopped) {
                Row {
                    TextButton(
                        onClick = {
                            onStopBed()
                            onDismiss()
                        }
                    ) {
                        Text("Stop bed")
                    }
                    TextButton(onClick = onDismiss) {
                        Text("Close")
                    }
                }
            } else {
                TextButton(onClick = onDismiss) {
                    Text("Close")
                }
            }
        },
    )
}

/** #4018: the bed's own volume, independent of the book; moves the playing bed live. */
@Composable
private fun BedVolumeRow(level: Float, onLevel: (Float) -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        // Local while dragging: the saved level comes back a beat later and would make the thumb stutter.
        var value by remember { mutableStateOf(level) }
        Text("Bed volume")
        Slider(
            value = value,
            onValueChange = { value = it; onLevel(it) },
            valueRange = 0f..1f,
            modifier = Modifier.weight(1f),
        )
    }
}
