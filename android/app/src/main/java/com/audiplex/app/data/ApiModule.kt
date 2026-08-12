package com.audiplex.app.data

import com.audiplex.app.data.api.AudiplexApi
import com.squareup.moshi.Moshi
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory
import java.io.IOException
import java.util.concurrent.TimeUnit
import javax.inject.Singleton

/**
 * The player's OkHttpClient is shared between the Retrofit API (audiplex
 * server only) and Media3's OkHttpDataSource (any playable URL, including
 * external streams like Radio Free Luna's /stream.mp3 via dj_play_stream).
 * Bearer-tokening every request through that shared client would leak
 * Todd's audiplex JWT to whatever external host a stream URL points at —
 * so the token (and the 401 -> clearAuthToken side effect) only applies
 * when the request host matches the configured audiplex server.
 */
class AuthInterceptor(
    private val settingsStore: AuthTokenStore,
    scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
) : Interceptor {

    /**
     * Token and server host are mirrored here so the hot path doesn't
     * runBlocking on DataStore twice per request. The mirrors are kept current
     * by collectors; a null mirror means they haven't emitted yet (very early
     * startup), and only then do we block for a first read.
     */
    @Volatile
    private var cachedToken: String? = null

    @Volatile
    private var cachedHost: String? = null

    init {
        scope.launch { settingsStore.authToken.collect { cachedToken = it } }
        scope.launch {
            settingsStore.serverUrl.collect { cachedHost = it.toHttpUrlOrNull()?.host ?: "" }
        }
    }

    private fun currentToken(): String {
        cachedToken?.let { return it }
        val token = runBlocking { settingsStore.authToken.first() }
        cachedToken = token
        return token
    }

    private fun currentHost(): String {
        cachedHost?.let { return it }
        val host = runBlocking { settingsStore.serverUrl.first() }.toHttpUrlOrNull()?.host ?: ""
        cachedHost = host
        return host
    }

    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val audiplexHost = currentHost()
        if (audiplexHost.isEmpty() || request.url.host != audiplexHost) {
            return chain.proceed(request)
        }

        val token = currentToken()
        val authedRequest = if (token.isNotBlank()) {
            request.newBuilder()
                .header("Authorization", "Bearer $token")
                .build()
        } else {
            request
        }
        val response = chain.proceed(authedRequest)

        // The server hands back a renewed token once one is past halfway
        // through its life, so an app that gets used never reaches the expiry
        // cliff that used to log Todd out roughly monthly.
        response.header(REFRESH_TOKEN_HEADER)?.takeIf { it.isNotBlank() }?.let { renewed ->
            cachedToken = renewed
            runBlocking { settingsStore.setAuthToken(renewed) }
        }

        if (response.code == 401 && !isCredentialSubmission(request)) {
            // A real rejection of a stored token: end the session, but record
            // WHY so the login screen can explain itself.
            cachedToken = ""
            runBlocking { settingsStore.expireSession() }
        }
        return response
    }

    /**
     * Login and registration answer 401 to mean "those credentials are wrong",
     * not "your stored session is dead". Clearing the token on those replies
     * meant one fat-fingered password wiped a perfectly good session.
     */
    private fun isCredentialSubmission(request: Request): Boolean =
        CREDENTIAL_PATHS.any { request.url.encodedPath.endsWith(it) }

    companion object {
        const val REFRESH_TOKEN_HEADER = "X-Refresh-Token"
        private val CREDENTIAL_PATHS = listOf("/api/auth/login", "/api/auth/register")
    }
}

/**
 * Retries idempotent GET requests that fail with a transient network error
 * (connection reset, read timeout, momentary Tailscale/Wi-Fi blip). Without
 * this, a single dropped packet while loading the library bubbles up as an
 * error and the screen goes blank until the user pulls to refresh. Only GETs
 * are retried so non-idempotent POST/PUT calls can't double-write.
 */
class RetryInterceptor(private val maxRetries: Int = 2) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        if (request.method != "GET") return chain.proceed(request)

        var lastError: IOException? = null
        for (attempt in 0..maxRetries) {
            try {
                return chain.proceed(request)
            } catch (e: IOException) {
                lastError = e
                if (attempt == maxRetries) break
                try {
                    Thread.sleep(300L * (attempt + 1))
                } catch (_: InterruptedException) {
                    Thread.currentThread().interrupt()
                    break
                }
            }
        }
        throw lastError ?: IOException("Request failed after $maxRetries retries")
    }
}

@Module
@InstallIn(SingletonComponent::class)
object ApiModule {

    @Provides
    @Singleton
    fun provideMoshi(): Moshi = Moshi.Builder().build()

    @Provides
    @Singleton
    fun provideOkHttpClient(settingsStore: SettingsStore): OkHttpClient {
        val auth = AuthInterceptor(settingsStore)
        val logging = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC
        }
        return OkHttpClient.Builder()
            .addInterceptor(RetryInterceptor())
            .addInterceptor(auth)
            .addInterceptor(logging)
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .build()
    }

    @Provides
    @Singleton
    fun provideApiServiceHolder(
        okHttpClient: OkHttpClient,
        moshi: Moshi
    ): ApiServiceHolder = ApiServiceHolder(okHttpClient, moshi)
}

class ApiServiceHolder(
    private val okHttpClient: OkHttpClient,
    private val moshi: Moshi
) {
    @Volatile
    private var currentBaseUrl: String = ""

    @Volatile
    private var currentApi: AudiplexApi? = null

    fun setBaseUrl(url: String) {
        val normalized = if (url.endsWith("/")) url else "$url/"
        if (normalized != currentBaseUrl) {
            currentBaseUrl = normalized
            currentApi = Retrofit.Builder()
                .baseUrl(normalized)
                .client(okHttpClient)
                .addConverterFactory(MoshiConverterFactory.create(moshi))
                .build()
                .create(AudiplexApi::class.java)
        }
    }

    val baseUrl: String get() = currentBaseUrl

    val api: AudiplexApi?
        get() = currentApi
}
