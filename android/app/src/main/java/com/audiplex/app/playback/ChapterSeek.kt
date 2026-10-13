package com.audiplex.app.playback

import com.audiplex.app.data.api.ChapterSchema

/**
 * #4219: the audiobook seek bar spans the CURRENT chapter, not the whole book, so a
 * drag in a 32-hour single-file book (Anathem) lands where Todd means it to.
 * Positions here are whole-book ms, the same as PlaybackManager.positionMs.
 */
object ChapterSeek {
    data class Span(val startMs: Long, val endMs: Long) {
        val lengthMs: Long get() = (endMs - startMs).coerceAtLeast(1L)

        /** Book position -> offset into this chapter, clamped to the chapter. */
        fun offsetOf(bookMs: Long): Long = (bookMs - startMs).coerceIn(0L, lengthMs)

        /** Offset into this chapter -> book position. */
        fun bookMsOf(offsetMs: Long): Long = startMs + offsetMs.coerceIn(0L, lengthMs)
    }

    /**
     * The chapter's span: its end is end_seconds, else the next chapter's start, else
     * the book's end. Null when the book has no usable chapter there (the bar then
     * spans the whole book, as before).
     */
    fun span(chapters: List<ChapterSchema>, index: Int, bookDurationMs: Long): Span? {
        val ch = chapters.getOrNull(index) ?: return null
        val start = (ch.startSeconds * 1000).toLong()
        val end = ch.endSeconds?.let { (it * 1000).toLong() }
            ?: chapters.getOrNull(index + 1)?.let { (it.startSeconds * 1000).toLong() }
            ?: bookDurationMs
        return if (end > start) Span(start, end) else null
    }
}

/** #4220: long-press a skip button to pick its size. Todd: past 5 min he jumps chapters. */
object SkipInterval {
    val CHOICES_S = listOf(10, 30, 60, 300)
    const val DEFAULT_S = 30  // what both buttons have always done (the "10" icon was wrong)

    fun coerce(seconds: Int?): Int = seconds?.takeIf { it in CHOICES_S } ?: DEFAULT_S

    fun label(seconds: Int): String = if (seconds < 60) "${seconds}s" else "${seconds / 60} min"
}
