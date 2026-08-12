package com.audiplex.app.data

import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Covers the three behaviors that made Todd's session disappear roughly
 * monthly: the token must be attached, a renewed token must be picked up, and
 * a 401 from the login endpoint must NOT be mistaken for the stored session
 * dying.
 */
class AuthInterceptorTest {

    /** In-memory stand-in for SettingsStore — no Context, no DataStore. */
    private class FakeTokenStore(
        serverUrl: String,
        token: String = ""
    ) : AuthTokenStore {
        private val urlFlow = MutableStateFlow(serverUrl)
        private val tokenFlow = MutableStateFlow(token)

        var expireCalls = 0
            private set

        override val serverUrl: Flow<String> get() = urlFlow
        override val authToken: Flow<String> get() = tokenFlow

        val currentToken: String get() = tokenFlow.value

        override suspend fun setAuthToken(token: String) {
            tokenFlow.value = token
        }

        override suspend fun expireSession() {
            expireCalls++
            tokenFlow.value = ""
        }
    }

    private lateinit var server: MockWebServer

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun clientFor(store: AuthTokenStore) = OkHttpClient.Builder()
        .addInterceptor(AuthInterceptor(store))
        .build()

    private fun call(client: OkHttpClient, path: String, method: String = "GET") {
        val builder = Request.Builder().url(server.url(path))
        if (method == "POST") {
            builder.post(ByteArray(0).toRequestBody())
        }
        client.newCall(builder.build()).execute().close()
    }

    @Test
    fun `attaches bearer token for the configured server`() = runTest {
        val store = FakeTokenStore(server.url("/").toString(), token = "stored-token")
        server.enqueue(MockResponse().setResponseCode(200))

        call(clientFor(store), "/api/auth/me")

        assertEquals("Bearer stored-token", server.takeRequest().getHeader("Authorization"))
    }

    @Test
    fun `does not attach token to a host that is not the audiplex server`() = runTest {
        // Server URL points somewhere else, so this request is an external
        // stream and must not carry Todd's JWT.
        val store = FakeTokenStore("http://not-the-server.invalid:8100/", token = "stored-token")
        server.enqueue(MockResponse().setResponseCode(200))

        call(clientFor(store), "/stream.mp3")

        assertNull(server.takeRequest().getHeader("Authorization"))
    }

    @Test
    fun `swaps in a renewed token from the refresh header`() = runTest {
        val store = FakeTokenStore(server.url("/").toString(), token = "old-token")
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader(AuthInterceptor.REFRESH_TOKEN_HEADER, "renewed-token")
        )

        call(clientFor(store), "/api/auth/me")

        assertEquals("renewed-token", store.currentToken)
        assertEquals(0, store.expireCalls)
    }

    @Test
    fun `a 401 on a normal request ends the session`() = runTest {
        val store = FakeTokenStore(server.url("/").toString(), token = "stale-token")
        server.enqueue(MockResponse().setResponseCode(401))

        call(clientFor(store), "/api/library/books")

        assertEquals(1, store.expireCalls)
        assertTrue(store.currentToken.isEmpty())
    }

    @Test
    fun `a 401 from the login endpoint does not wipe a valid session`() = runTest {
        // The regression this guards: one mistyped password used to delete a
        // perfectly good stored token.
        val store = FakeTokenStore(server.url("/").toString(), token = "good-token")
        server.enqueue(MockResponse().setResponseCode(401))

        call(clientFor(store), "/api/auth/login", method = "POST")

        assertEquals(0, store.expireCalls)
        assertEquals("good-token", store.currentToken)
    }

    @Test
    fun `a 401 from the register endpoint does not wipe a valid session`() = runTest {
        val store = FakeTokenStore(server.url("/").toString(), token = "good-token")
        server.enqueue(MockResponse().setResponseCode(401))

        call(clientFor(store), "/api/auth/register", method = "POST")

        assertEquals(0, store.expireCalls)
        assertEquals("good-token", store.currentToken)
    }

    @Test
    fun `sends no auth header when there is no stored token`() = runTest {
        val store = FakeTokenStore(server.url("/").toString(), token = "")
        server.enqueue(MockResponse().setResponseCode(401))

        call(clientFor(store), "/api/library/books")

        assertNull(server.takeRequest().getHeader("Authorization"))
        assertFalse(store.currentToken.isNotEmpty())
    }
}
