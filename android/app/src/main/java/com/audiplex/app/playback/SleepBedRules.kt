package com.audiplex.app.playback

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
     * #4018: the bed's own level, independent of the book; previously fixed at 0.5.
     * #5131: 0.45 on the new scale is the old 0.9 (player volume 0.9, no boost).
     */
    const val DEFAULT_VOLUME = 0.45f

    /** #5131: the slider position that equals the old maximum (player volume 1.0, no boost). */
    const val UNITY_LEVEL = 0.5f

    /** #5131: LoudnessEnhancer gain at the top of the slider; its limiter guards it. */
    const val MAX_BOOST_DB = 12f

    /** #5131: what one slider level asks of the bed player and its LoudnessEnhancer. */
    data class BedGain(val volume: Float, val boostDb: Float)

    /**
     * #5131: the bottom half of the slider is player volume 0..1 at 0 dB; the top
     * half holds volume at 1 and adds 0..+12 dB, linear in dB. NaN reads as 0.
     */
    fun bedGain(level: Float): BedGain {
        val l = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        return if (l <= UNITY_LEVEL) BedGain(l / UNITY_LEVEL, 0f)
        else BedGain(1f, (l - UNITY_LEVEL) / (1f - UNITY_LEVEL) * MAX_BOOST_DB)
    }

    /** #5131: DJ bed levels keep their old 0..1 meaning (1 = player volume 1), so they never boost. */
    fun fromDjLevel(djLevel: Float): Float =
        if (djLevel.isNaN()) 0f else djLevel.coerceIn(0f, 1f) * UNITY_LEVEL

    /**
     * #5131: one-time rescale of the saved slider value, so last night's level sounds
     * the same. Returns the value to write, or null when there is nothing to write;
     * the caller sets the migrated flag either way.
     */
    fun migrateStoredLevel(stored: Float?, alreadyMigrated: Boolean): Float? =
        if (alreadyMigrated || stored == null) null else stored.coerceIn(0f, 1f) * UNITY_LEVEL

    /** #5131: a bed that starts at a level fades in over this long, never cold at full boost. */
    const val RAMP_MS = 8_000L
    const val RAMP_STEPS = 32

    /** #5131: only a boosted start fades in; DJ levels (chimes ride bed_play) never boost, so never wait. */
    fun needsRampIn(level: Float): Boolean = bedGain(level).boostDb > 0f

    /** #5131: [steps] levels after [from], ending exactly at [to] (clamped 0..1). */
    fun rampLevels(from: Float, to: Float, steps: Int = RAMP_STEPS): List<Float> {
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
