package com.audiplex.app.ui.settings

import android.content.Context
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.BuildConfig
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.data.api.MusicRoot
import com.audiplex.app.data.api.MusicRootsUpdate
import com.audiplex.app.data.download.DownloadRepository
import com.audiplex.app.update.ApkInstaller
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import javax.inject.Inject

sealed class ConnectionTestState {
    data object Idle : ConnectionTestState()
    data object Testing : ConnectionTestState()
    data object Success : ConnectionTestState()
    data class Failed(val message: String) : ConnectionTestState()
}

sealed class MusicRootsState {
    data object Idle : MusicRootsState()
    /** Saving the new folder list and rescanning the library. */
    data object Saving : MusicRootsState()
    data class Failed(val message: String) : MusicRootsState()
}

sealed class UpdateState {
    data object Idle : UpdateState()
    data object Checking : UpdateState()
    data class UpToDate(val versionName: String, val versionCode: Int) : UpdateState()
    data class Available(val versionName: String, val versionCode: Int, val sizeBytes: Long) : UpdateState()
    data class Downloading(val downloadedBytes: Long, val totalBytes: Long) : UpdateState()
    data object Installing : UpdateState()
    data class Failed(val message: String) : UpdateState()
}

@HiltViewModel
class SettingsViewModel @Inject constructor(
    @ApplicationContext private val context: Context,
    private val settingsStore: SettingsStore,
    private val apiHolder: ApiServiceHolder,
    private val downloadRepository: DownloadRepository
) : ViewModel() {

    private val _serverUrl = MutableStateFlow("")
    val serverUrl: StateFlow<String> = _serverUrl

    private val _testState = MutableStateFlow<ConnectionTestState>(ConnectionTestState.Idle)
    val testState: StateFlow<ConnectionTestState> = _testState

    private val _saved = MutableStateFlow(false)
    val saved: StateFlow<Boolean> = _saved

    private val _updateState = MutableStateFlow<UpdateState>(UpdateState.Idle)
    val updateState: StateFlow<UpdateState> = _updateState

    private val _musicRoots = MutableStateFlow<List<MusicRoot>>(emptyList())
    val musicRoots: StateFlow<List<MusicRoot>> = _musicRoots

    private val _musicRootsState = MutableStateFlow<MusicRootsState>(MusicRootsState.Idle)
    val musicRootsState: StateFlow<MusicRootsState> = _musicRootsState

    val installedVersionName: String get() = BuildConfig.VERSION_NAME
    val installedVersionCode: Int get() = BuildConfig.VERSION_CODE

    val downloadOnCellular: StateFlow<Boolean> = settingsStore.downloadOnCellular
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), false)

    val username: StateFlow<String> = settingsStore.username
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), "")

    val totalStorageUsed: StateFlow<Long> = downloadRepository.totalStorageUsed()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), 0L)

    fun toggleDownloadOnCellular() {
        viewModelScope.launch {
            val current = downloadOnCellular.value
            settingsStore.setDownloadOnCellular(!current)
        }
    }

    fun logout() {
        viewModelScope.launch {
            settingsStore.clearAuthToken()
            settingsStore.clearUsername()
        }
    }

    init {
        viewModelScope.launch {
            val url = settingsStore.serverUrl.first()
            _serverUrl.value = url
            if (url.isNotBlank()) {
                apiHolder.setBaseUrl(url)
                loadMusicRoots()
            }
        }
    }

    fun loadMusicRoots() {
        viewModelScope.launch {
            val api = apiHolder.api ?: return@launch
            runCatching { api.getMusicRoots() }
                .onSuccess { _musicRoots.value = it.roots }
        }
    }

    fun addMusicRoot(path: String) {
        val trimmed = path.trim()
        if (trimmed.isBlank()) return
        if (_musicRoots.value.any { it.path.equals(trimmed, ignoreCase = true) }) return
        saveMusicRoots(_musicRoots.value.map { it.path } + trimmed)
    }

    fun removeMusicRoot(path: String) {
        saveMusicRoots(_musicRoots.value.map { it.path }.filterNot { it == path })
    }

    /** Persist the new folder set; the server rescans before returning, so this
     *  can take a moment on a large library. */
    private fun saveMusicRoots(paths: List<String>) {
        viewModelScope.launch {
            val api = apiHolder.api ?: return@launch
            _musicRootsState.value = MusicRootsState.Saving
            runCatching { api.setMusicRoots(MusicRootsUpdate(paths)) }
                .onSuccess {
                    _musicRoots.value = it.roots
                    _musicRootsState.value = MusicRootsState.Idle
                }
                .onFailure {
                    _musicRootsState.value =
                        MusicRootsState.Failed(it.message ?: "Failed to save folders")
                }
        }
    }

    fun setUrl(url: String) {
        _serverUrl.value = url
        _testState.value = ConnectionTestState.Idle
        _saved.value = false
    }

    fun testConnection() {
        viewModelScope.launch {
            _testState.value = ConnectionTestState.Testing
            try {
                val url = _serverUrl.value
                apiHolder.setBaseUrl(url)
                val api = apiHolder.api ?: throw IllegalStateException("Failed to create API client")
                val health = api.health()
                if (health.status == "ok") {
                    _testState.value = ConnectionTestState.Success
                } else {
                    _testState.value = ConnectionTestState.Failed("Unexpected response: ${health.status}")
                }
            } catch (e: Exception) {
                _testState.value = ConnectionTestState.Failed(e.message ?: "Connection failed")
            }
        }
    }

    fun save() {
        viewModelScope.launch {
            settingsStore.setServerUrl(_serverUrl.value)
            apiHolder.setBaseUrl(_serverUrl.value)
            _saved.value = true
        }
    }

    fun checkForUpdate() {
        viewModelScope.launch {
            _updateState.value = UpdateState.Checking
            try {
                apiHolder.setBaseUrl(_serverUrl.value)
                val api = apiHolder.api ?: throw IllegalStateException("No API client")
                val info = api.getAppVersion()
                _updateState.value = if (info.versionCode > installedVersionCode) {
                    UpdateState.Available(info.versionName, info.versionCode, info.sizeBytes)
                } else {
                    UpdateState.UpToDate(info.versionName, info.versionCode)
                }
            } catch (e: Exception) {
                _updateState.value = UpdateState.Failed(e.message ?: "Check failed")
            }
        }
    }

    fun downloadAndInstall() {
        val available = _updateState.value as? UpdateState.Available ?: return
        viewModelScope.launch {
            try {
                _updateState.value = UpdateState.Downloading(0L, available.sizeBytes)
                val api = apiHolder.api ?: throw IllegalStateException("No API client")
                val response = api.downloadApk()
                if (!response.isSuccessful) {
                    _updateState.value = UpdateState.Failed("HTTP ${response.code()}")
                    return@launch
                }
                val body = response.body() ?: run {
                    _updateState.value = UpdateState.Failed("Empty response")
                    return@launch
                }

                val outFile = File(ApkInstaller.apkCacheDir(context), "audiplex-${available.versionCode}.apk")

                withContext(Dispatchers.IO) {
                    body.byteStream().use { input ->
                        outFile.outputStream().use { output ->
                            val buffer = ByteArray(64 * 1024)
                            var total = 0L
                            while (true) {
                                val n = input.read(buffer)
                                if (n == -1) break
                                output.write(buffer, 0, n)
                                total += n
                                _updateState.value = UpdateState.Downloading(total, available.sizeBytes)
                            }
                        }
                    }
                }

                _updateState.value = UpdateState.Installing
                ApkInstaller.launchInstall(context, outFile)
            } catch (e: Exception) {
                _updateState.value = UpdateState.Failed(e.message ?: "Download failed")
            }
        }
    }
}
