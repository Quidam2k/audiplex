package com.audiplex.app.playback

import com.audiplex.app.data.api.ChapterSchema
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * #4219 and #4220: tests for chapter seeking and selectable skip intervals.
 */
class ChapterSeekTest {

    @Test
    fun span_uses_explicit_end_seconds() {
        val chapters = listOf(
            ChapterSchema(0, "First", 0.0, 600.0),
            ChapterSchema(1, "Second", 600.0, 1500.0)
        )

        val span = requireNotNull(ChapterSeek.span(chapters, 1, 2_000_000L))

        assertEquals(ChapterSeek.Span(600_000L, 1_500_000L), span)
        assertEquals(900_000L, span.lengthMs)
    }

    @Test
    fun span_missing_end_uses_next_chapter_start() {
        val chapters = listOf(
            ChapterSchema(0, "First", 0.0, null),
            ChapterSchema(1, "Second", 600.0, 1500.0)
        )

        assertEquals(
            ChapterSeek.Span(0L, 600_000L),
            ChapterSeek.span(chapters, 0, 2_000_000L)
        )
    }

    @Test
    fun span_last_chapter_missing_end_uses_book_duration() {
        val chapters = listOf(
            ChapterSchema(0, "First", 0.0, 600.0),
            ChapterSchema(1, "Second", 600.0, null)
        )

        assertEquals(
            ChapterSeek.Span(600_000L, 2_000_000L),
            ChapterSeek.span(chapters, 1, 2_000_000L)
        )
    }

    @Test
    fun span_out_of_range_index_returns_null() {
        val chapters = listOf(
            ChapterSchema(0, "First", 0.0, 600.0),
            ChapterSchema(1, "Second", 600.0, 1500.0)
        )

        assertNull(ChapterSeek.span(chapters, -1, 2_000_000L))
        assertNull(ChapterSeek.span(chapters, chapters.size, 2_000_000L))
    }

    @Test
    fun span_empty_chapters_returns_null() {
        assertNull(ChapterSeek.span(emptyList(), 0, 2_000_000L))
    }

    @Test
    fun span_zero_length_chapter_returns_null() {
        val chapters = listOf(
            ChapterSchema(0, "Empty", 600.0, 600.0)
        )

        assertNull(ChapterSeek.span(chapters, 0, 2_000_000L))
    }

    @Test
    fun span_end_before_start_returns_null() {
        val chapters = listOf(
            ChapterSchema(0, "Invalid", 600.0, 500.0)
        )

        assertNull(ChapterSeek.span(chapters, 0, 2_000_000L))
    }

    @Test
    fun offsetOf_clamps_to_chapter_bounds() {
        val span = ChapterSeek.Span(600_000L, 1_500_000L)

        assertEquals(0L, span.offsetOf(500_000L))
        assertEquals(0L, span.offsetOf(span.startMs))
        assertEquals(300_000L, span.offsetOf(900_000L))
        assertEquals(span.lengthMs, span.offsetOf(span.endMs))
        assertEquals(span.lengthMs, span.offsetOf(1_600_000L))
    }

    @Test
    fun bookMsOf_adds_start_and_clamps_to_chapter_bounds() {
        val span = ChapterSeek.Span(600_000L, 1_500_000L)

        assertEquals(span.startMs, span.bookMsOf(-100_000L))
        assertEquals(span.startMs, span.bookMsOf(0L))
        assertEquals(900_000L, span.bookMsOf(300_000L))
        assertEquals(span.endMs, span.bookMsOf(span.lengthMs))
        assertEquals(span.endMs, span.bookMsOf(1_000_000L))
    }

    @Test
    fun bookMsOf_offsetOf_round_trip_preserves_positions_inside_chapter() {
        val span = ChapterSeek.Span(600_000L, 1_500_000L)
        val positions = listOf(
            600_000L,
            600_001L,
            900_123L,
            1_499_999L,
            1_500_000L
        )

        for (position in positions) {
            assertEquals(position, span.bookMsOf(span.offsetOf(position)))
        }
    }

    @Test
    fun coerce_null_and_unsupported_values_use_default() {
        assertEquals(30, SkipInterval.DEFAULT_S)
        assertEquals(30, SkipInterval.coerce(null))
        assertEquals(SkipInterval.DEFAULT_S, SkipInterval.coerce(null))
        assertEquals(30, SkipInterval.coerce(45))
    }

    @Test
    fun coerce_preserves_every_choice() {
        assertEquals(listOf(10, 30, 60, 300), SkipInterval.CHOICES_S)
        assertEquals(300, SkipInterval.coerce(300))

        for (seconds in SkipInterval.CHOICES_S) {
            assertEquals(seconds, SkipInterval.coerce(seconds))
        }
    }

    @Test
    fun label_formats_seconds_and_minutes() {
        assertEquals("10s", SkipInterval.label(10))
        assertEquals("30s", SkipInterval.label(30))
        assertEquals("1 min", SkipInterval.label(60))
        assertEquals("5 min", SkipInterval.label(300))
    }
}
