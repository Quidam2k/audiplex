package com.audiplex.app.playback

import androidx.media3.common.PlaybackException
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** #ride0928: the auto-skip decision for a music file that fails to play. */
class SkipFailedTrackTest {
    private val badStatus = PlaybackException.ERROR_CODE_IO_BAD_HTTP_STATUS

    private fun decide(
        code: Int = badStatus,
        kind: PlayerKind? = PlayerKind.Music,
        id: Int? = 7,
        next: Boolean = true,
        recent: Int = 0,
    ) = shouldSkipFailedTrack(code, kind, id, next, recent)

    @Test
    fun `io error on a music track with a next item skips`() {
        assertTrue(decide())
        assertTrue(decide(code = PlaybackException.ERROR_CODE_IO_FILE_NOT_FOUND))
    }

    @Test
    fun `non io errors do not skip`() {
        assertFalse(decide(code = PlaybackException.ERROR_CODE_DECODING_FAILED))
        assertFalse(decide(code = PlaybackException.ERROR_CODE_UNSPECIFIED))
        // #ride0928: a Tailscale blip is not a missing file; skipping would eat the queue
        assertFalse(decide(code = PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_FAILED))
        assertFalse(decide(code = PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_TIMEOUT))
    }

    @Test
    fun `audiobooks streams and unknown kind do not skip`() {
        assertFalse(decide(kind = PlayerKind.Audiobook))
        assertFalse(decide(kind = PlayerKind.Stream))
        assertFalse(decide(kind = null))
    }

    @Test
    fun `dj voice clips with negative ids do not skip`() {
        assertFalse(decide(id = -3))
        assertFalse(decide(id = null))
    }

    @Test
    fun `no next item means no skip`() {
        assertFalse(decide(next = false))
    }

    @Test
    fun `skip loop guard stops after too many recent failures`() {
        assertTrue(decide(recent = SKIP_LOOP_MAX_FAILURES - 1))
        assertFalse(decide(recent = SKIP_LOOP_MAX_FAILURES))
    }
}
