package com.audiplex.app.ui.auth

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.data.api.LoginRequest
import com.audiplex.app.data.api.RegisterRequest
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import javax.inject.Inject

sealed class AuthError {
    data class Message(val text: String) : AuthError()
}

@HiltViewModel
class LoginViewModel @Inject constructor(
    private val settingsStore: SettingsStore,
    private val apiHolder: ApiServiceHolder
) : ViewModel() {

    val isLoggedIn: StateFlow<Boolean?> = settingsStore.authToken
        .map { token -> token.isNotBlank() }
        .stateIn(viewModelScope, SharingStarted.Eagerly, null)

    private val _error = MutableStateFlow<AuthError?>(null)
    val error: StateFlow<AuthError?> = _error

    private val _loading = MutableStateFlow(false)
    val loading: StateFlow<Boolean> = _loading

    fun login(username: String, password: String) {
        viewModelScope.launch {
            _loading.value = true
            _error.value = null
            try {
                val api = apiHolder.api ?: throw IllegalStateException("Server URL not configured")
                val resp = api.login(LoginRequest(username, password))
                settingsStore.setAuthToken(resp.token)
                settingsStore.setUsername(resp.user.username)
            } catch (e: Exception) {
                _error.value = AuthError.Message(e.message ?: "Login failed")
            } finally {
                _loading.value = false
            }
        }
    }

    fun register(username: String, password: String, displayName: String?) {
        viewModelScope.launch {
            _loading.value = true
            _error.value = null
            try {
                val api = apiHolder.api ?: throw IllegalStateException("Server URL not configured")
                val resp = api.register(RegisterRequest(username, password, displayName))
                settingsStore.setAuthToken(resp.token)
                settingsStore.setUsername(resp.user.username)
            } catch (e: Exception) {
                _error.value = AuthError.Message(e.message ?: "Registration failed")
            } finally {
                _loading.value = false
            }
        }
    }

    fun clearError() {
        _error.value = null
    }
}
