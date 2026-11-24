package com.audiplex.app.playback

import com.audiplex.app.playback.MediaKeyRules.Action
import com.audiplex.app.playback.MediaKeyRules.Decision
import com.audiplex.app.playback.MediaKeyRules.Key
import org.junit.Assert.*
import org.junit.Test

class MediaKeyRulesTest {

    private fun decide(
        key: Key,
        taps: Int = 1,
        playing: Boolean = false,
        msSinceRouteChange: Long? = null,
        otherAppPlaying: Boolean = false
    ): Decision = MediaKeyRules.decide(key, taps, playing, msSinceRouteChange, otherAppPlaying)

    // keyOf

    @Test
    fun `keyOf maps headset hook 79 to play pause`() {
        assertEquals(Key.PLAY_PAUSE, MediaKeyRules.keyOf(79))
    }

    @Test
    fun `keyOf maps media play pause 85 to play pause`() {
        assertEquals(Key.PLAY_PAUSE, MediaKeyRules.keyOf(85))
    }

    @Test
    fun `keyOf maps 126 to play`() {
        assertEquals(Key.PLAY, MediaKeyRules.keyOf(126))
    }

    @Test
    fun `keyOf maps 127 to pause`() {
        assertEquals(Key.PAUSE, MediaKeyRules.keyOf(127))
    }

    @Test
    fun `keyOf returns null for unrelated code`() {
        assertNull(MediaKeyRules.keyOf(87))
    }

    @Test
    fun `route guard constant is three seconds`() {
        assertEquals(3_000L, MediaKeyRules.ROUTE_GUARD_MS)
    }

    // PAUSE key while paused

    @Test
    fun `pause key on a paused player plays`() {
        assertEquals(
            Decision(Action.PLAY, "pause_key_while_paused"),
            decide(Key.PAUSE, playing = false)
        )
    }

    @Test
    fun `pause key ten seconds after connect on a paused player plays`() {
        assertEquals(
            Decision(Action.PLAY, "pause_key_while_paused"),
            decide(Key.PAUSE, playing = false, msSinceRouteChange = 10_000L)
        )
    }

    @Test
    fun `pause key while paused at guard start 0 is ignored`() {
        assertEquals(
            Decision(Action.IGNORE, "route_guard"),
            decide(Key.PAUSE, msSinceRouteChange = 0L)
        )
    }

    @Test
    fun `pause key while paused at 2999 is ignored`() {
        assertEquals(
            Decision(Action.IGNORE, "route_guard"),
            decide(Key.PAUSE, msSinceRouteChange = 2_999L)
        )
    }

    @Test
    fun `pause key while paused at exactly 3000 is outside the guard`() {
        assertEquals(
            Decision(Action.PLAY, "pause_key_while_paused"),
            decide(Key.PAUSE, msSinceRouteChange = 3_000L)
        )
    }

    @Test
    fun `pause key while paused with null route change is outside the guard`() {
        assertEquals(
            Decision(Action.PLAY, "pause_key_while_paused"),
            decide(Key.PAUSE, msSinceRouteChange = null)
        )
    }

    @Test
    fun `pause key while paused with negative route change is outside the guard`() {
        assertEquals(
            Decision(Action.PLAY, "pause_key_while_paused"),
            decide(Key.PAUSE, msSinceRouteChange = -5L)
        )
    }

    @Test
    fun `route guard wins over other app playing`() {
        assertEquals(
            Decision(Action.IGNORE, "route_guard"),
            decide(Key.PAUSE, msSinceRouteChange = 1_000L, otherAppPlaying = true)
        )
    }

    @Test
    fun `pause key while paused and other app playing is ignored`() {
        assertEquals(
            Decision(Action.IGNORE, "other_app_playing"),
            decide(Key.PAUSE, msSinceRouteChange = 10_000L, otherAppPlaying = true)
        )
    }

    @Test
    fun `other app playing ignored with null route change`() {
        assertEquals(
            Decision(Action.IGNORE, "other_app_playing"),
            decide(Key.PAUSE, msSinceRouteChange = null, otherAppPlaying = true)
        )
    }

    @Test
    fun `other app playing at exactly 3000 is ignored as other app`() {
        assertEquals(
            Decision(Action.IGNORE, "other_app_playing"),
            decide(Key.PAUSE, msSinceRouteChange = 3_000L, otherAppPlaying = true)
        )
    }

    // PAUSE key while playing

    @Test
    fun `pause key while playing pauses`() {
        assertEquals(
            Decision(Action.PAUSE, "pause_key"),
            decide(Key.PAUSE, playing = true)
        )
    }

    @Test
    fun `pause key while playing pauses even inside the route guard`() {
        assertEquals(
            Decision(Action.PAUSE, "pause_key"),
            decide(Key.PAUSE, playing = true, msSinceRouteChange = 100L)
        )
    }

    @Test
    fun `pause key while playing pauses inside guard with other app playing`() {
        assertEquals(
            Decision(Action.PAUSE, "pause_key"),
            decide(Key.PAUSE, playing = true, msSinceRouteChange = 100L, otherAppPlaying = true)
        )
    }

    // PLAY_PAUSE

    @Test
    fun `play pause key on a paused player plays`() {
        assertEquals(
            Decision(Action.PLAY, "toggle"),
            decide(Key.PLAY_PAUSE, playing = false)
        )
    }

    @Test
    fun `play pause key on a playing player pauses`() {
        assertEquals(
            Decision(Action.PAUSE, "toggle"),
            decide(Key.PLAY_PAUSE, playing = true)
        )
    }

    @Test
    fun `play pause key ignores route guard when paused`() {
        assertEquals(
            Decision(Action.PLAY, "toggle"),
            decide(Key.PLAY_PAUSE, playing = false, msSinceRouteChange = 0L)
        )
    }

    @Test
    fun `play pause key ignores other app playing when paused`() {
        assertEquals(
            Decision(Action.PLAY, "toggle"),
            decide(Key.PLAY_PAUSE, playing = false, otherAppPlaying = true)
        )
    }

    @Test
    fun `play pause key ignores both guards together`() {
        assertEquals(
            Decision(Action.PLAY, "toggle"),
            decide(Key.PLAY_PAUSE, playing = false, msSinceRouteChange = 500L, otherAppPlaying = true)
        )
    }

    @Test
    fun `play pause key pauses while playing inside guard`() {
        assertEquals(
            Decision(Action.PAUSE, "toggle"),
            decide(Key.PLAY_PAUSE, playing = true, msSinceRouteChange = 500L, otherAppPlaying = true)
        )
    }

    // PLAY

    @Test
    fun `play key on a paused player plays`() {
        assertEquals(
            Decision(Action.PLAY, "play_key"),
            decide(Key.PLAY, playing = false)
        )
    }

    @Test
    fun `play key ignores guards when paused`() {
        assertEquals(
            Decision(Action.PLAY, "play_key"),
            decide(Key.PLAY, playing = false, msSinceRouteChange = 0L, otherAppPlaying = true)
        )
    }

    @Test
    fun `play key while playing is ignored`() {
        assertEquals(
            Decision(Action.IGNORE, "already_playing"),
            decide(Key.PLAY, playing = true)
        )
    }

    @Test
    fun `play key while playing is ignored inside guard`() {
        assertEquals(
            Decision(Action.IGNORE, "already_playing"),
            decide(Key.PLAY, playing = true, msSinceRouteChange = 10L, otherAppPlaying = true)
        )
    }

    // Multi-tap, all keys

    @Test
    fun `zero taps are ignored for every key`() {
        for (key in Key.values()) {
            assertEquals(Decision(Action.IGNORE, "no_taps"), decide(key, taps = 0))
        }
    }

    @Test
    fun `negative taps are ignored for every key`() {
        for (key in Key.values()) {
            assertEquals(Decision(Action.IGNORE, "no_taps"), decide(key, taps = -1))
        }
    }

    @Test
    fun `double tap goes next for every key`() {
        for (key in Key.values()) {
            for (playing in listOf(true, false)) {
                assertEquals(
                    Decision(Action.NEXT, "double_tap"),
                    decide(key, taps = 2, playing = playing)
                )
            }
        }
    }

    @Test
    fun `triple tap forwards 30 for every key`() {
        for (key in Key.values()) {
            for (playing in listOf(true, false)) {
                assertEquals(
                    Decision(Action.FORWARD_30, "triple_tap"),
                    decide(key, taps = 3, playing = playing)
                )
            }
        }
    }

    @Test
    fun `four taps are ignored for every key`() {
        for (key in Key.values()) {
            assertEquals(Decision(Action.IGNORE, "too_many_taps"), decide(key, taps = 4))
        }
    }

    @Test
    fun `many taps are ignored for every key`() {
        for (key in Key.values()) {
            assertEquals(Decision(Action.IGNORE, "too_many_taps"), decide(key, taps = 10, playing = true))
        }
    }

    @Test
    fun `multi tap on pause key ignores route guard and other app`() {
        assertEquals(
            Decision(Action.NEXT, "double_tap"),
            decide(Key.PAUSE, taps = 2, msSinceRouteChange = 0L, otherAppPlaying = true)
        )
        assertEquals(
            Decision(Action.FORWARD_30, "triple_tap"),
            decide(Key.PAUSE, taps = 3, msSinceRouteChange = 0L, otherAppPlaying = true)
        )
    }
    // TapCounter (#3249: discrete keys act at once and close an open run)

    @Test
    fun `play pause taps wait for the window`() {
        val c = MediaKeyRules.TapCounter()
        assertTrue(c.press(Key.PLAY_PAUSE).isEmpty())
        assertTrue(c.press(Key.PLAY_PAUSE).isEmpty())
        assertTrue(c.pending)
        assertEquals(MediaKeyRules.Press(Key.PLAY_PAUSE, 2), c.flush())
        assertFalse(c.pending)
        assertNull(c.flush())
    }

    @Test
    fun `discrete pause acts at once`() {
        val c = MediaKeyRules.TapCounter()
        assertEquals(listOf(MediaKeyRules.Press(Key.PAUSE, 1)), c.press(Key.PAUSE))
        assertFalse(c.pending)
    }

    @Test
    fun `two discrete pauses are two single presses, never a double tap`() {
        val c = MediaKeyRules.TapCounter()
        assertEquals(listOf(MediaKeyRules.Press(Key.PAUSE, 1)), c.press(Key.PAUSE))
        assertEquals(listOf(MediaKeyRules.Press(Key.PAUSE, 1)), c.press(Key.PAUSE))
    }

    @Test
    fun `a discrete key closes an open play pause run first`() {
        val c = MediaKeyRules.TapCounter()
        c.press(Key.PLAY_PAUSE)
        assertEquals(
            listOf(MediaKeyRules.Press(Key.PLAY_PAUSE, 1), MediaKeyRules.Press(Key.PLAY, 1)),
            c.press(Key.PLAY),
        )
        assertFalse(c.pending)
    }

    @Test
    fun `pause then play pause does not add up to a skip`() {
        val c = MediaKeyRules.TapCounter()
        c.press(Key.PAUSE)
        c.press(Key.PLAY_PAUSE)
        assertEquals(MediaKeyRules.Press(Key.PLAY_PAUSE, 1), c.flush())
    }
}
