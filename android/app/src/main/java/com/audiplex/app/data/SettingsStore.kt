package com.audiplex.app.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.longPreferencesKey
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
    private val djLinkEnabledKey = booleanPreferencesKey("dj_link_enabled")
    // v2: the v1 key was advanced by a reporter that dropped every entry it
    // "reported" (no API client existed that early in startup), so the deaths
    // it was meant to capture were marked done without ever being sent. A new
    // key resets the watermark to zero and re-ships the history Android still
    // holds on the device (#2961).
    // v3 (#3021): bumping the key resets the watermark so the exit history
    // Android still holds — including the 2026-08-14 16:02:58Z death — re-ships
    // WITH its stack trace, which the v2 records were delivered without.
    private val lastExitReportedAtKey = longPreferencesKey("last_exit_reported_at_v3")

    override val serverUrl: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[serverUrlKey] ?: ""
    }

    val downloadOnCellular: Flow<Boolean> = context.dataStore.data.map { prefs ->
        prefs[downloadOnCellularKey] ?: false
    }

    /**
     * Whether the always-on DJ link service runs (#3022).
     *
     * Defaults ON per Todd's ruling, and stays a plain toggle on purpose: his
     * framing was "we can change our mind later if it ends up not being a good
     * idea", so turning this off must return the app to exactly its previous
     * behaviour rather than disabling DJ control. Nothing may become
     * load-bearing on the service being up.
     */
    val djLinkEnabled: Flow<Boolean> = context.dataStore.data.map { prefs ->
        prefs[djLinkEnabledKey] ?: true
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

    /**
     * Timestamp of the newest process-exit record already shipped to the
     * server, so a restart reports each death exactly once (#2961).
     */
    val lastExitReportedAt: Flow<Long> = context.dataStore.data.map { prefs ->
        prefs[lastExitReportedAtKey] ?: 0L
    }

    suspend fun setLastExitReportedAt(timestamp: Long) {
        context.dataStore.edit { prefs ->
            prefs[lastExitReportedAtKey] = timestamp
        }
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

    suspend fun setDjLinkEnabled(enabled: Boolean) {
        context.dataStore.edit { prefs ->
            prefs[djLinkEnabledKey] = enabled
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
