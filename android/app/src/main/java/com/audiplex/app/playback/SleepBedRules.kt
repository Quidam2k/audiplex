package com.audiplex.app.playback

import kotlin.math.log10
import kotlin.math.pow

/** #4018: whether the sleep bed is wanted, running, recovering, or permanently stopped. */
enum class BedState {
    /** #4018: no bed is wanted; it was never started or the user stopped it. */
    Off,

    /** #4018: the bed is wanted and playing or buffering toward playing. */
    Playing,

    /** #4018: the bed is wanted, the player errored, and a retry or fallback is scheduled. */
    Recovering,

    /**
     * #4018: the bed is wanted but dead for good; retries and fallback are exhausted,
     * or the player ended or idled without the user stopping it.
     */
    Stopped,
}

/** #4018: pure decisions for keeping the looping sleep bed alive overnight. */
object SleepBedRules {

    /**
     * #4018: the bed's own level, independent of the book.
     * #5282: levels are plain player volume 0..1. Nothing goes past unity; the phone is the master.
     */
    const val DEFAULT_VOLUME = 0.9f

    /** #5282: "Book at bedtime" starts at the book's own level: no cut until he asks for one. */
    const val DEFAULT_BOOK_LEVEL = 1f

    /** #5282: what the sliders span, on a dB taper so a drag sounds even. Position 0 is silent. */
    private const val SLIDER_RANGE_DB = 40.0

    fun sliderToLevel(position: Float): Float {
        val p = if (position.isNaN()) 0f else position.coerceIn(0f, 1f)
        return if (p <= 0f) 0f else 10.0.pow((p - 1.0) * SLIDER_RANGE_DB / 20.0).toFloat()
    }

    fun levelToSlider(level: Float): Float {
        val l = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        return if (l <= 0f) 0f else (1.0 + 20.0 * log10(l.toDouble()) / SLIDER_RANGE_DB).coerceIn(0.0, 1.0).toFloat()
    }

    /** DJ bed levels are the same 0..1 player volume. NaN reads as 0. */
    fun fromDjLevel(djLevel: Float): Float =
        if (djLevel.isNaN()) 0f else djLevel.coerceIn(0f, 1f)

    /** #5282: the bed and the bedtime book after the one-time move off the #5131 boost scale. */
    data class BedtimeMix(val bedLevel: Float, val bookLevel: Float)

    /**
     * #5282: keep the balance he last set. On the #5131 scale 0.5 was player volume 1 and 1.0 was +12 dB.
     * A boost becomes the same cut on the book, so bed over book stays where it was. Null: nothing to
     * write. A phone that never ran #5131's rescale still holds plain player volume, so it stays as is.
     */
    fun migrateToAttenuate(stored: Float?, headroomMigrated: Boolean, alreadyMigrated: Boolean): BedtimeMix? {
        if (alreadyMigrated || !headroomMigrated || stored == null) return null
        val l = if (stored.isNaN()) 0f else stored.coerceIn(0f, 1f)
        if (l <= OLD_UNITY) return BedtimeMix(l / OLD_UNITY, DEFAULT_BOOK_LEVEL)
        val boostDb = (l - OLD_UNITY) / (1f - OLD_UNITY) * OLD_MAX_BOOST_DB
        return BedtimeMix(1f, 10.0.pow(-boostDb / 20.0).toFloat())
    }

    private const val OLD_UNITY = 0.5f
    private const val OLD_MAX_BOOST_DB = 12f

    /** #5282: the bedtime level as a player multiplier; anything out of range reads as silent or full. */
    fun trimAmplitude(level: Float): Float = if (level.isNaN()) 1f else level.coerceIn(0f, 1f)

    /**
     * #5282: book gain plus the bedtime level while it applies, never past 0 dB, so no limiter
     * runs at bedtime. A level of 0 is -infinity, which the caller's pow() turns into silence.
     */
    fun bookNetDb(gainDb: Double, trimLevel: Float): Double {
        val trim = trimAmplitude(trimLevel)
        if (trim <= 0f) return Double.NEGATIVE_INFINITY
        return (gainDb + 20.0 * log10(trim.toDouble())).coerceAtMost(0.0)
    }

    /**
     * #5282: the phone volume and output when bedtime started. Raising the phone at bedtime
     * would otherwise leave the next book or song louder than he left it, the morning blare
     * (msg 38353). Android keeps one volume per output, so another output is not affected.
     */
    data class SleepHold(val index: Int, val route: String, val atMs: Long = 0L, val finished: Boolean = false)

    /**
     * How long after bedtime ends the hold lasts if he never comes back down. Covers the next morning
     * and no further, so a headset raised at night does not stay quiet on tomorrow evening's ride.
     */
    const val HOLD_MAX_MS = 16 * 60 * 60_000L

    private fun holdExpired(hold: SleepHold, nowMs: Long): Boolean = nowMs - hold.atMs >= HOLD_MAX_MS

    /** Same output and still above where bedtime started: Audiplex keeps its bedtime cut. */
    fun holdActive(hold: SleepHold?, index: Int, route: String, nowMs: Long = 0L): Boolean =
        hold != null && hold.route == route && index > hold.index && !holdExpired(hold, nowMs)

    /** Back down to where bedtime started on that output, or past its time: the hold is done. */
    fun holdCleared(hold: SleepHold, index: Int, route: String, nowMs: Long = 0L): Boolean =
        holdExpired(hold, nowMs) || (hold.route == route && index <= hold.index)

    /** A later bedtime on the same output keeps the lower start, so last night's raise is not locked in. */
    fun nextHold(existing: SleepHold?, index: Int, route: String, nowMs: Long = 0L): SleepHold =
        if (existing != null && existing.route == route && !holdExpired(existing, nowMs)) {
            SleepHold(minOf(existing.index, index), route, nowMs)
        } else {
            SleepHold(index, route, nowMs)
        }

    /**
     * Bedtime is over. No raise means no hold, so a preview or a cancelled timer leaves nothing
     * behind to quiet a later, deliberate level. A raise starts the clock. Null: nothing to keep.
     */
    fun finishHold(hold: SleepHold, index: Int, route: String, nowMs: Long): SleepHold? = when {
        hold.finished -> hold
        holdCleared(hold, index, route, nowMs) -> null
        else -> hold.copy(atMs = nowMs, finished = true)
    }

    const val BOOK_RAMP_MS = 2_000L
    const val BOOK_RAMP_STEPS = 16

    /** #5282: [steps] volumes after [from], ending exactly at [to]. */
    fun rampVolumes(from: Float, to: Float, steps: Int = BOOK_RAMP_STEPS): List<Float> {
        val n = steps.coerceAtLeast(1)
        val end = to.coerceIn(0f, 1f)
        return (1..n).map { i -> if (i == n) end else (from + (to - from) * i / n).coerceIn(0f, 1f) }
    }

    /** #4018: retry delays for each bed, including the fallback. */
    val RETRY_DELAYS_MS = listOf(10_000L, 30_000L, 60_000L)

    /** #4018: playing this long since the last (re)start earns a fresh retry budget. */
    const val HEALTHY_RESET_MS = 5 * 60_000L

    /** #4018: the next action after a bed player error. */
    sealed interface Recovery {
        /** #4018: restart the current bed after [delayMs]. */
        data class Retry(val delayMs: Long) : Recovery

        /** #4018: switch to the fallback bed with a fresh retry counter. */
        object Fallback : Recovery

        /** #4018: no retries or fallback remain. */
        object GiveUp : Recovery
    }

    /**
     * #4018: [failures] counts errors including the one just seen, starting at one.
     * Values below one are treated as one. The caller resets the counter when
     * switching to the fallback, which receives the same retry ladder.
     */
    fun recoveryFor(failures: Int, onFallback: Boolean, hasFallback: Boolean): Recovery {
        val failureCount = failures.coerceAtLeast(1)
        return when {
            failureCount <= RETRY_DELAYS_MS.size ->
                Recovery.Retry(RETRY_DELAYS_MS[failureCount - 1])
            !onFallback && hasFallback -> Recovery.Fallback
            else -> Recovery.GiveUp
        }
    }

    // Mirror androidx.media3.common.Player.STATE_* without an Android dependency.
    private const val STATE_IDLE = 1
    private const val STATE_BUFFERING = 2
    private const val STATE_READY = 3
    private const val STATE_ENDED = 4

    /**
     * #4018: map player flags to the bed's state. A freshly prepared player with
     * [playWhenReady] set is Playing while buffering, including during startup.
     */
    fun stateFor(
        wanted: Boolean,
        recovering: Boolean,
        playWhenReady: Boolean,
        playbackState: Int,
    ): BedState = when {
        !wanted -> BedState.Off
        recovering -> BedState.Recovering
        playWhenReady && (playbackState == STATE_BUFFERING || playbackState == STATE_READY) ->
            BedState.Playing
        else -> BedState.Stopped
    }

    /**
     * #4018: PlaybackService keeps the media notification foreground while the bed
     * plays or is being restarted, even with the book paused; otherwise Android
     * kills the process minutes after the book's sleep fade pauses it (10/10 01:32).
     */
    fun foregroundRequired(mediaRequired: Boolean, bed: BedState): Boolean =
        mediaRequired || bed == BedState.Playing || bed == BedState.Recovering

    /**
     * #4018: key the local bed copy on the URL path so LAN and tailnet URLs share
     * one file. Ignore the scheme, host, port, query, and fragment; relative paths
     * work too. The sanitized path is capped at 80 characters after the "bed_" prefix.
     */
    fun cacheFileName(url: String): String {
        val path = url.substringBefore('?')
            .substringBefore('#')
            .replace(Regex("^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//[^/]*"), "")
        val name = path.replace(Regex("[^A-Za-z0-9]+"), "_")
            .trim('_')
            .take(80)
            .ifEmpty { "root" }
        return "bed_$name"
    }

    /** #4018: the brown-noise bed is the overnight fallback. */
    fun isFallbackTitle(title: String): Boolean =
        title.contains("brown", ignoreCase = true)

    /** #4018: choose the first brown-noise bed, otherwise the last bed, or null when empty. */
    fun pickFallback(beds: List<Pair<String, String>>): String? =
        beds.firstOrNull { isFallbackTitle(it.first) }?.second ?: beds.lastOrNull()?.second
}
