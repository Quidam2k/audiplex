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
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import com.audiplex.app.data.api.SleepBed

/** #3714 in-app sleep button */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SleepDialog(
    beds: List<SleepBed>,
    minutesLeft: Int?,
    bedPlaying: Boolean,
    onStart: (minutes: Int, bed: SleepBed?) -> Unit,
    onExtend: () -> Unit,
    onCancel: () -> Unit,
    onStopBed: () -> Unit,
    onDismiss: () -> Unit,
) {
    var minutes by rememberSaveable { mutableStateOf(30) }
    // Keyed on the default bed: the list loads after the dialog opens, and the
    // default must follow it instead of sticking on Silence.
    var selectedBedId by rememberSaveable(beds.firstOrNull()?.id) {
        mutableStateOf<Int?>(beds.firstOrNull()?.id)
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

                bedPlaying -> Text("Sleep bed is playing.")

                else -> {
                    Column(
                        modifier = Modifier.verticalScroll(rememberScrollState()),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        Text("Keep the book playing, then fade into:")

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

                bedPlaying -> {
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
                            )
                            onDismiss()
                        }
                    ) {
                        Text("Start")
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
            } else {
                TextButton(onClick = onDismiss) {
                    Text("Close")
                }
            }
        },
    )
}
