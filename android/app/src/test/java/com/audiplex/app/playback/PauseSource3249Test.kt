package com.audiplex.app.playback

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** #3249: a session controller names a pause only when nobody nearer did, and only briefly. */
class PauseSource3249Test {

    @After
    fun clear() {
        PlayerHooks.takePauseSource(0L)
    }

    @Test
    fun `a fresh session command names the pause`() {
        PlayerHooks.noteSessionCommand("session:com.android.systemui", 1_000L)
        assertEquals("session:com.android.systemui", PlayerHooks.takePauseSource(1_400L))
        assertNull(PlayerHooks.takePauseSource(1_500L))
    }

    @Test
    fun `a note from the pauser wins over the session command`() {
        PlayerHooks.noteSessionCommand("session:com.audiplex.app", 1_000L)
        PlayerHooks.notePause("app_ui")
        assertEquals("app_ui", PlayerHooks.takePauseSource(1_100L))
        // The session note is spent too, so it can't label the next pause.
        assertNull(PlayerHooks.takePauseSource(1_200L))
    }

    @Test
    fun `a stale session command names nothing`() {
        PlayerHooks.noteSessionCommand("session:com.audiplex.app", 1_000L)
        assertNull(PlayerHooks.takePauseSource(1_000L + PlayerHooks.SESSION_SOURCE_TTL_MS + 1))
    }

    @Test
    fun `a clock that went backwards names nothing`() {
        PlayerHooks.noteSessionCommand("session:x", 5_000L)
        assertNull(PlayerHooks.takePauseSource(4_000L))
    }
}
