package com.audiplex.app.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import javax.inject.Inject
import javax.inject.Singleton

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "settings")

@Singleton
class SettingsStore @Inject constructor(
    @ApplicationContext private val context: Context
) : AuthTokenStore {
    private val serverUrlKey = stringPreferencesKey("server_url")
    private val downloadOnCellularKey = booleanPreferencesKey("download_on_cellular")
    private val authTokenKey = stringPreferencesKey("auth_token")
    private val usernameKey = stringPreferencesKey("username")
    private val sessionExpiredKey = booleanPreferencesKey("session_expired")

    override val serverUrl: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[serverUrlKey] ?: ""
    }

    val downloadOnCellular: Flow<Boolean> = context.dataStore.data.map { prefs ->
        prefs[downloadOnCellularKey] ?: false
    }

    override val authToken: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[authTokenKey] ?: ""
    }

    val username: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[usernameKey] ?: ""
    }

    /**
     * True when the session ended because the server rejected our token,
     * rather than because the user chose to sign out. Lets the login screen
     * explain itself instead of appearing for no visible reason — the old
     * behavior, which is how a silently-expiring token looked like a bug.
     */
    val sessionExpired: Flow<Boolean> = context.dataStore.data.map { prefs ->
        prefs[sessionExpiredKey] ?: false
    }

    suspend fun setServerUrl(url: String) {
        context.dataStore.edit { prefs ->
            prefs[serverUrlKey] = url
        }
    }

    suspend fun setDownloadOnCellular(enabled: Boolean) {
        context.dataStore.edit { prefs ->
            prefs[downloadOnCellularKey] = enabled
        }
    }

    override suspend fun setAuthToken(token: String) {
        context.dataStore.edit { prefs ->
            prefs[authTokenKey] = token
            // Any successfully obtained token settles the expiry notice.
            prefs.remove(sessionExpiredKey)
        }
    }

    suspend fun clearAuthToken() {
        context.dataStore.edit { prefs ->
            prefs.remove(authTokenKey)
            prefs.remove(sessionExpiredKey)
        }
    }

    /**
     * Drop the token because the server rejected it, flagging the reason so
     * the login screen can say "session expired" rather than just appearing.
     */
    override suspend fun expireSession() {
        context.dataStore.edit { prefs ->
            prefs.remove(authTokenKey)
            prefs[sessionExpiredKey] = true
        }
    }

    suspend fun clearSessionExpired() {
        context.dataStore.edit { prefs ->
            prefs.remove(sessionExpiredKey)
        }
    }

    suspend fun setUsername(name: String) {
        context.dataStore.edit { prefs ->
            prefs[usernameKey] = name
        }
    }

    suspend fun clearUsername() {
        context.dataStore.edit { prefs ->
            prefs.remove(usernameKey)
        }
    }
}
