package com.audiplex.app.data

import kotlinx.coroutines.flow.Flow

/**
 * The slice of [SettingsStore] that [AuthInterceptor] actually needs.
 *
 * Exists so the interceptor's auth behavior — which token it attaches, when it
 * swaps in a renewed one, and which 401s are allowed to end the session — can
 * be unit tested on the JVM without a Context or a real DataStore.
 */
interface AuthTokenStore {
    val serverUrl: Flow<String>
    val authToken: Flow<String>

    suspend fun setAuthToken(token: String)

    /** Drop the token because the server rejected it, flagging why. */
    suspend fun expireSession()
}
