package com.audiplex.app.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    onBack: () -> Unit,
    onDownloadsClick: () -> Unit = {},
    onLogout: () -> Unit = {},
    viewModel: SettingsViewModel = hiltViewModel()
) {
    val serverUrl by viewModel.serverUrl.collectAsState()
    val testState by viewModel.testState.collectAsState()
    val saved by viewModel.saved.collectAsState()
    val updateState by viewModel.updateState.collectAsState()
    val downloadOnCellular by viewModel.downloadOnCellular.collectAsState()
    val totalStorageUsed by viewModel.totalStorageUsed.collectAsState()
    val username by viewModel.username.collectAsState()
    val musicRoots by viewModel.musicRoots.collectAsState()
    val musicRootsState by viewModel.musicRootsState.collectAsState()
    val installedVersion = remember { "${viewModel.installedVersionName} (${viewModel.installedVersionCode})" }

    Column(modifier = Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text("Settings") },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                }
            },
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
            Text(
                "Server URL",
                style = MaterialTheme.typography.titleMedium
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = serverUrl,
                onValueChange = { viewModel.setUrl(it) },
                label = { Text("Server URL") },
                placeholder = { Text("http://192.168.1.100:8000") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )

            Spacer(Modifier.height(16.dp))

            OutlinedButton(
                onClick = { viewModel.testConnection() },
                modifier = Modifier.fillMaxWidth(),
                enabled = testState !is ConnectionTestState.Testing && serverUrl.isNotBlank()
            ) {
                when (testState) {
                    is ConnectionTestState.Testing -> {
                        CircularProgressIndicator(
                            modifier = Modifier.height(20.dp),
                            strokeWidth = 2.dp
                        )
                    }
                    else -> Text("Test Connection")
                }
            }

            when (val state = testState) {
                is ConnectionTestState.Success -> {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "Connected successfully!",
                        color = MaterialTheme.colorScheme.primary,
                        style = MaterialTheme.typography.bodyMedium
                    )
                }
                is ConnectionTestState.Failed -> {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "Connection failed: ${state.message}",
                        color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.bodyMedium
                    )
                }
                else -> {}
            }

            Spacer(Modifier.height(24.dp))

            Button(
                onClick = {
                    viewModel.save()
                    onBack()
                },
                modifier = Modifier.fillMaxWidth(),
                enabled = serverUrl.isNotBlank()
            ) {
                Text("Save")
            }

            if (saved) {
                Spacer(Modifier.height(8.dp))
                Text(
                    "Settings saved",
                    color = MaterialTheme.colorScheme.primary,
                    style = MaterialTheme.typography.bodySmall
                )
            }

            Spacer(Modifier.height(32.dp))
            HorizontalDivider()
            Spacer(Modifier.height(16.dp))

            MusicFoldersSection(
                roots = musicRoots,
                state = musicRootsState,
                onAdd = { viewModel.addMusicRoot(it) },
                onRemove = { viewModel.removeMusicRoot(it) }
            )

            Spacer(Modifier.height(32.dp))
            HorizontalDivider()
            Spacer(Modifier.height(16.dp))

            Text(
                "Downloads",
                style = MaterialTheme.typography.titleMedium
            )
            Spacer(Modifier.height(8.dp))
            OutlinedButton(
                onClick = onDownloadsClick,
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("Downloads · ${formatFileSize(totalStorageUsed)}")
            }
            Spacer(Modifier.height(12.dp))
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("Download over cellular", style = MaterialTheme.typography.bodyMedium)
                Switch(
                    checked = downloadOnCellular,
                    onCheckedChange = { viewModel.toggleDownloadOnCellular() }
                )
            }

            Spacer(Modifier.height(32.dp))
            HorizontalDivider()
            Spacer(Modifier.height(16.dp))

            Text(
                "Account",
                style = MaterialTheme.typography.titleMedium
            )
            Spacer(Modifier.height(8.dp))
            if (username.isNotBlank()) {
                Text(
                    "Logged in as $username",
                    style = MaterialTheme.typography.bodyMedium
                )
            }
            Spacer(Modifier.height(12.dp))
            OutlinedButton(
                onClick = { viewModel.logout(); onLogout() },
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("Log Out")
            }

            Spacer(Modifier.height(32.dp))
            HorizontalDivider()
            Spacer(Modifier.height(16.dp))

            Text(
                "App version",
                style = MaterialTheme.typography.titleMedium
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "Installed: $installedVersion",
                style = MaterialTheme.typography.bodyMedium
            )
            Spacer(Modifier.height(12.dp))

            UpdateSection(
                state = updateState,
                onCheck = { viewModel.checkForUpdate() },
                onInstall = { viewModel.downloadAndInstall() }
            )
        }
    }
}

@Composable
private fun UpdateSection(
    state: UpdateState,
    onCheck: () -> Unit,
    onInstall: () -> Unit
) {
    when (state) {
        is UpdateState.Idle, is UpdateState.UpToDate, is UpdateState.Failed -> {
            OutlinedButton(
                onClick = onCheck,
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("Check for Update")
            }
        }
        is UpdateState.Checking -> {
            OutlinedButton(
                onClick = {},
                enabled = false,
                modifier = Modifier.fillMaxWidth()
            ) {
                CircularProgressIndicator(
                    modifier = Modifier.height(20.dp),
                    strokeWidth = 2.dp
                )
            }
        }
        is UpdateState.Available -> {
            Button(
                onClick = onInstall,
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("Download & install ${state.versionName} (${(state.sizeBytes / 1024 / 1024.0).format(1)} MB)")
            }
        }
        is UpdateState.Downloading -> {
            val progress = if (state.totalBytes > 0) {
                state.downloadedBytes.toFloat() / state.totalBytes.toFloat()
            } else 0f
            LinearProgressIndicator(
                progress = { progress },
                modifier = Modifier.fillMaxWidth()
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "Downloading… ${(state.downloadedBytes / 1024 / 1024.0).format(1)} / ${(state.totalBytes / 1024 / 1024.0).format(1)} MB",
                style = MaterialTheme.typography.bodySmall
            )
        }
        is UpdateState.Installing -> {
            Text(
                "Launching installer…",
                style = MaterialTheme.typography.bodyMedium
            )
        }
    }

    when (state) {
        is UpdateState.UpToDate -> {
            Spacer(Modifier.height(8.dp))
            Text(
                "You're on the latest (${state.versionName}).",
                color = MaterialTheme.colorScheme.primary,
                style = MaterialTheme.typography.bodyMedium
            )
        }
        is UpdateState.Failed -> {
            Spacer(Modifier.height(8.dp))
            Text(
                "Update check failed: ${state.message}",
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodyMedium
            )
        }
        else -> {}
    }
}

@Composable
private fun MusicFoldersSection(
    roots: List<com.audiplex.app.data.api.MusicRoot>,
    state: MusicRootsState,
    onAdd: (String) -> Unit,
    onRemove: (String) -> Unit
) {
    var newPath by remember { mutableStateOf("") }
    val busy = state is MusicRootsState.Saving

    Text("Music Folders", style = MaterialTheme.typography.titleMedium)
    Spacer(Modifier.height(4.dp))
    Text(
        "Folders scanned to populate the Music section.",
        style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant
    )
    Spacer(Modifier.height(8.dp))

    if (roots.isEmpty()) {
        Text(
            "No music folders configured.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
    roots.forEach { root ->
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(root.path, style = MaterialTheme.typography.bodyMedium)
                if (!root.exists) {
                    Text(
                        "Not found on server",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error
                    )
                }
            }
            IconButton(
                onClick = { onRemove(root.path) },
                enabled = !busy
            ) {
                Icon(Icons.Default.Close, contentDescription = "Remove folder")
            }
        }
    }

    Spacer(Modifier.height(8.dp))
    OutlinedTextField(
        value = newPath,
        onValueChange = { newPath = it },
        label = { Text("Add folder path") },
        placeholder = { Text("E:/Music") },
        singleLine = true,
        enabled = !busy,
        modifier = Modifier.fillMaxWidth()
    )
    Spacer(Modifier.height(8.dp))
    Button(
        onClick = {
            onAdd(newPath)
            newPath = ""
        },
        enabled = newPath.isNotBlank() && !busy,
        modifier = Modifier.fillMaxWidth()
    ) {
        Text("Add folder")
    }

    when (state) {
        is MusicRootsState.Saving -> {
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(
                    modifier = Modifier.height(20.dp),
                    strokeWidth = 2.dp
                )
                Spacer(Modifier.height(0.dp))
                Text(
                    "  Saving & rescanning library…",
                    style = MaterialTheme.typography.bodyMedium
                )
            }
        }
        is MusicRootsState.Failed -> {
            Spacer(Modifier.height(8.dp))
            Text(
                "Couldn't save folders: ${state.message}",
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodyMedium
            )
        }
        else -> {}
    }
}

private fun Double.format(decimals: Int): String = "%.${decimals}f".format(this)

private fun formatFileSize(bytes: Long): String {
    val gb = bytes / (1024.0 * 1024.0 * 1024.0)
    if (gb >= 1.0) return "%.1f GB".format(gb)
    val mb = bytes / (1024.0 * 1024.0)
    if (mb >= 1.0) return "%.0f MB".format(mb)
    return "0 MB"
}
