package com.audiplex.app.playback

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.SystemClock

/**
 * Pantheon #6707 (Todd msg 66044): Pantheon's Talk has a ducking dial per mic type. Android
 * focus carries no depth, so Pantheon sends an explicit DUCK_LEVEL broadcast (a fraction of
 * our own dial level) just before each focus request it makes. A level applies to the
 * CAN_DUCK it precedes (fresh within [freshMs]) and holds for that whole duck episode (a repeat
 * CAN_DUCK while still ducked keeps it, Jarvis condition 2) until focus comes back. No fresh
 * level and no episode -> null, i.e. FocusPolicy.DUCK_VOLUME, today's 5%. Pure; clock passed in.
 */
class ExternalDuckLevel(private val freshMs: Long = FRESH_MS) {
    private var pending: Float? = null
    private var pendingAtMs = 0L
    private var episode: Float? = null

    @Synchronized fun offer(level: Float, nowMs: Long) {
        if (!level.isFinite()) return
        pending = level.coerceIn(MIN, MAX)
        pendingAtMs = nowMs
    }

    /** Fraction of the dial level for a CAN_DUCK arriving now, or null for the 5% default. */
    @Synchronized fun forDuck(nowMs: Long): Float? {
        val p = pending
        if (p != null && nowMs - pendingAtMs in 0..freshMs) {
            pending = null
            episode = p
            return p
        }
        pending = null
        return episode
    }

    /** Focus came back (GAIN) or was given up: the episode is over. */
    @Synchronized fun endEpisode() { episode = null }

    companion object {
        const val FRESH_MS = 3_000L
        const val MIN = 0.02f
        const val MAX = 0.6f
        const val ACTION = "com.audiplex.app.DUCK_LEVEL"  // Pantheon TalkDuckPolicy.DUCK_LEVEL_ACTION
        const val EXTRA_LEVEL = "level"
        val shared = ExternalDuckLevel()
    }
}

/** Manifest receiver for Pantheon's explicit (setPackage) DUCK_LEVEL broadcast. */
class DuckLevelReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ExternalDuckLevel.ACTION) return
        ExternalDuckLevel.shared.offer(intent.getFloatExtra(ExternalDuckLevel.EXTRA_LEVEL, Float.NaN), SystemClock.elapsedRealtime())
    }
}
