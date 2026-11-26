package com.audiplex.app.ui.settings

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.audiplex.app.ui.common.HomeAction

/**
 * #7044 (Todd 68920: "an easy to access universal panel for all of the volume sliders"):
 * the #5282/#5422 levels, on their own screen. Opened from the sliders icon (#7044) beside every
 * gear, from Settings, and from Pantheon via the com.audiplex.app.OPEN_LEVELS action.
 * Voices stay a link to the Pantheon app: the two apps are signed with different keys, so
 * a live cross-app slider would need an unprotected receiver (Jarvis's hard stop, asg 15024).
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LevelsScreen(
    onBack: () -> Unit,
    viewModel: SettingsViewModel = hiltViewModel()
) {
    val context = LocalContext.current
    val mixLevels by viewModel.mixLevels.collectAsState()
    val bedtimeShiftCappedDb by viewModel.bedtimeShiftCappedDb.collectAsState()
    val evenOutLoudness by viewModel.evenOutLoudness.collectAsState()
    Column(modifier = Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text("Volumes") },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                }
            },
            actions = { HomeAction() },
            colors = TopAppBarDefaults.topAppBarColors(
                containerColor = MaterialTheme.colorScheme.surface
            )
        )
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .verticalScroll(rememberScrollState())
                .padding(16.dp)
        ) {
            LevelsSection(
                levels = mixLevels,
                bedtimeShiftCappedDb = bedtimeShiftCappedDb,
                onChange = viewModel::setMixLevel,
                evenOut = evenOutLoudness,
                onEvenOutChange = viewModel::setEvenOutLoudness,
                onOpenVoices = if (remember { PantheonVoices.launchIntent(context) } == null) null
                    else ({ PantheonVoices.open(context) })  // resolved again on tap
            )
        }
    }
}
