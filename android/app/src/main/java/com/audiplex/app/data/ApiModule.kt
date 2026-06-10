package com.audiplex.app.data

import com.audiplex.app.data.api.AudiplexApi
import com.squareup.moshi.Moshi
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory
import java.io.IOException
import java.util.concurrent.TimeUnit
import javax.inject.Singleton

class AuthInterceptor(private val settingsStore: SettingsStore) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val token = runBlocking { settingsStore.authToken.first() }
        val request = if (token.isNotBlank()) {
            chain.request().newBuilder()
                .addHeader("Authorization", "Bearer $token")
                .build()
        } else {
            chain.request()
        }
        val response = chain.proceed(request)
        if (response.code == 401) {
            runBlocking { settingsStore.clearAuthToken() }
        }
        return response
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
