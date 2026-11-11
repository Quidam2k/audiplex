package com.audiplex.app.playback

/**
 * DJ clip edges, pure so the JVM tests pin them (#5836 step 3, #5837 F).
 *
 * - then="pause" (#5836): an outro announce arms [PauseAfter] for its clip. When the clip
 *   starts, the service sets ExoPlayer's one-shot pauseAtEndOfMediaItems, so the player stops
 *   at the clip's end and the next song never starts. Leaving the clip while that flag is
 *   still set means the stop did not happen: on an automatic advance, fade out over the
 *   payload's fade_ms and pause; on a skip (Todd's own), just drop it.
 * - Hold (#5837 F): a DJ clip that starts while Audiplex is ducked under another app's
 *   audio (a persona line) waits paused for the GAIN, at most [HOLD_CAP_MS].
 */
object DjClipRules {
    const val CLIP_PREFIX = "djclip:"
    const val HOLD_CAP_MS = 20_000L
    const val DEFAULT_FADE_MS = 3_000L

    data class PauseAfter(val mediaId: String, val fadeMs: Long)

    sealed interface Edge {
        data object None : Edge
        data object ArmPauseAtEnd : Edge
        data object CancelPauseAtEnd : Edge
        data class FadeAndPause(val fadeMs: Long) : Edge
    }

    fun isDjClip(mediaId: String?): Boolean = ClipEdgeFade.isClip(mediaId)

    fun clipMediaId(clipId: Long): String = "$CLIP_PREFIX$clipId"

    /** What a media-item transition from [from] to [to] does to an armed then="pause". */
    fun onTransition(
        from: String?,
        to: String?,
        auto: Boolean,
        armed: PauseAfter?,
        pauseAtEndSet: Boolean,
    ): Edge = when {
        armed == null -> Edge.None
        to == armed.mediaId -> Edge.ArmPauseAtEnd
        from == armed.mediaId && pauseAtEndSet -> if (auto) Edge.FadeAndPause(armed.fadeMs) else Edge.CancelPauseAtEnd
        else -> Edge.None
    }

    /** The armed then="pause" after the transition: spent once we leave its clip. */
    fun armedAfter(from: String?, to: String?, armed: PauseAfter?): PauseAfter? =
        if (armed != null && from == armed.mediaId && to != armed.mediaId) null else armed

    /** #5837 F: hold a DJ clip that is starting under another app's duck. */
    fun shouldHold(to: String?, ducked: Boolean, playWhenReady: Boolean): Boolean =
        isDjClip(to) && ducked && playWhenReady

    /** fade_ms from the payload, clamped to something sane; missing = [DEFAULT_FADE_MS]. */
    fun fadeMs(payloadFadeMs: Long?): Long = (payloadFadeMs ?: DEFAULT_FADE_MS).coerceIn(0L, 10_000L)
}
