package com.audiplex.app.playback

/**
 * #3249: Decides what a headset media key press should do once the
 * MediaSession callback has counted taps inside its 500 ms window.
 * #3249: Pure JVM on purpose, so the rules can be unit tested without Android.
 */
object MediaKeyRules {
    /** #3249: A headset that sends PAUSE by itself as it connects or disconnects must not start music. */
    const val ROUTE_GUARD_MS = 3_000L

    /** #3249: Logical keys we handle; everything else is left to the session. */
    enum class Key { PLAY_PAUSE, PLAY, PAUSE }

    /** #3249: What the player should do in response to the key press. */
    enum class Action { PLAY, PAUSE, NEXT, FORWARD_30, IGNORE }

    /** #3249: [why] is a short stable reason string, handy for logs and tests. */
    data class Decision(val action: Action, val why: String)

    /** #3249: Maps 79/85 to PLAY_PAUSE, 126 to PLAY, 127 to PAUSE, else null (session handles it). */
    fun keyOf(keyCode: Int): Key? = when (keyCode) {
        79, 85 -> Key.PLAY_PAUSE
        126 -> Key.PLAY
        127 -> Key.PAUSE
        else -> null
    }

    /**
     * #3249: [msSinceRouteChange] is null when no audio device was ever added or removed.
     * #3249: [playing] means the player wants to play.
     */
    fun decide(
        key: Key,
        taps: Int,
        playing: Boolean,
        msSinceRouteChange: Long?,
        otherAppPlaying: Boolean,
    ): Decision = when {
        taps <= 0 -> Decision(Action.IGNORE, "no_taps")
        taps == 2 -> Decision(Action.NEXT, "double_tap")
        taps == 3 -> Decision(Action.FORWARD_30, "triple_tap")
        taps > 3 -> Decision(Action.IGNORE, "too_many_taps")
        else -> when (key) {
            Key.PLAY_PAUSE ->
                if (playing) Decision(Action.PAUSE, "toggle") else Decision(Action.PLAY, "toggle")
            Key.PLAY ->
                if (playing) Decision(Action.IGNORE, "already_playing")
                else Decision(Action.PLAY, "play_key")
            Key.PAUSE -> decideSinglePause(playing, msSinceRouteChange, otherAppPlaying)
        }
    }

    /** #3249: one key press, or a run of PLAY_PAUSE taps, ready for [decide]. */
    data class Press(val key: Key, val taps: Int)

    /**
     * #3249: only PLAY_PAUSE (the one-button headset key) counts taps. Discrete PLAY / PAUSE act at
     * once, as they did before this ticket, and close any tap run still open, so an automatic
     * PAUSE followed by a real press can never add up to a double-tap skip.
     */
    class TapCounter {
        private var taps = 0

        /** True while a PLAY_PAUSE run waits for its window to close. */
        val pending: Boolean get() = taps > 0

        /** What to dispatch now, in order. */
        fun press(key: Key): List<Press> {
            if (key == Key.PLAY_PAUSE) {
                taps++
                return emptyList()
            }
            return listOfNotNull(flush()) + Press(key, 1)
        }

        /** The window closed: the run so far, or null if none. */
        fun flush(): Press? = if (taps > 0) Press(Key.PLAY_PAUSE, taps).also { taps = 0 } else null
    }

    private fun decideSinglePause(
        playing: Boolean,
        msSinceRouteChange: Long?,
        otherAppPlaying: Boolean,
    ): Decision {
        if (playing) return Decision(Action.PAUSE, "pause_key")
        // Fresh headsets send PAUSE on connect; the user's press must still unpause once the guard passes.
        // A negative value (clock went backwards) is deliberately outside the guard.
        if (msSinceRouteChange != null && msSinceRouteChange in 0 until ROUTE_GUARD_MS) {
            return Decision(Action.IGNORE, "route_guard")
        }
        // Don't steal audio focus from another app that is already playing.
        if (otherAppPlaying) return Decision(Action.IGNORE, "other_app_playing")
        return Decision(Action.PLAY, "pause_key_while_paused")
    }
}
