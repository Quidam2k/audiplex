package com.audiplex.app.playback

/**
 * #5695: the pure rules for a DJ clip played OVER the end of a song (bus command
 * overlay_clip). The phone schedules the clip against the MAIN player's own position, so
 * network jitter never shifts it: start once [startAtMs] into [forTrackId] while playing;
 * drop if that track is gone first; under another app's duck, wait for the gain at most
 * [DjClipRules.HOLD_CAP_MS]. JVM-tested in OverlaySessionTest.
 */
object OverlayRules {
    const val DEFAULT_DUCK_TO = 0.35f
    const val MIN_DUCK_TO = 0.05f

    const val UNKNOWN_CAP_MS = 20_000L
    const val CAP_SLACK_MS = 2_000L
    const val MAX_CAP_MS = 60_000L

    // #5695 a clip never waits longer than this for its start point
    const val MAX_SCHEDULE_MS = 10 * 60_000L

    data class View(
        val mediaId: String?,
        val positionMs: Long,
        val playing: Boolean,
        val ducked: Boolean,
    )

    sealed interface Step {
        data object Wait : Step
        data object Start : Step
        data class Drop(val reason: String) : Step
    }

    fun duckTo(payload: Float?): Float {
        // #5695 NaN would slip through coerceIn, treat it as absent
        val v = if (payload == null || payload.isNaN()) DEFAULT_DUCK_TO else payload
        return v.coerceIn(MIN_DUCK_TO, 1f)
    }

    fun restoreCapMs(clipSeconds: Double?): Long {
        if (clipSeconds == null || !clipSeconds.isFinite() || clipSeconds <= 0.0) {
            return UNKNOWN_CAP_MS
        }
        // #5695 clamp in double space first so huge values cannot overflow the Long
        val clipMs = (clipSeconds * 1000.0).coerceAtMost(MAX_CAP_MS.toDouble()).toLong()
        return (clipMs + CAP_SLACK_MS).coerceAtMost(MAX_CAP_MS)
    }

    fun step(
        view: View,
        forTrackId: Int,
        startAtMs: Long,
        armedAtMs: Long,
        dueSinceMs: Long?,
        nowMs: Long,
    ): Step {
        if (view.mediaId != forTrackId.toString()) return Step.Drop("track_changed")
        if (nowMs - armedAtMs > MAX_SCHEDULE_MS) return Step.Drop("never_reached")
        if (view.positionMs < startAtMs || !view.playing) return Step.Wait
        if (view.ducked) {
            return if (dueSinceMs != null && nowMs - dueSinceMs > DjClipRules.HOLD_CAP_MS) {
                Step.Drop("ducked_timeout")
            } else {
                Step.Wait
            }
        }
        return Step.Start
    }
}
