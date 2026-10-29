package com.audiplex.app.ui.library

import com.audiplex.app.data.api.BookSummary
import java.time.Instant
import org.junit.Assert.*
import org.junit.Test

class LibraryGroupingTest {
    private fun book(
        id: Int,
        title: String,
        author: String? = "A",
        series: String? = null,
        seq: String? = null,
        addedAt: String = "2026-02-01T00:00:00"
    ): BookSummary = BookSummary(
        id = id,
        title = title,
        author = author,
        narrator = null,
        series = series,
        seriesSequence = seq,
        durationSeconds = 0.0,
        hasCover = false,
        addedAt = addedAt
    )

    @Test
    fun seqKeyOrdersNumericSequencesAndPlacesMissingValuesLast() {
        val keys = listOf("2", "2.5", "3", "10").map { seqKey(it) }

        assertTrue(keys.zipWithNext().all { (a, b) -> a < b })
        assertEquals(2.5, seqKey(" 2.5 "), 0.0)
        assertEquals(Double.POSITIVE_INFINITY, seqKey(null), 0.0)
        assertEquals(Double.POSITIVE_INFINITY, seqKey("x"), 0.0)
    }

    @Test
    fun groupBySeriesUsesReadingOrderAndSkipsBooksWithoutSeries() {
        val series = "Dungeon Crawler Carl"
        val novels = listOf(
            book(5, "The Butcher's Masquerade", series = series, seq = "5"),
            book(2, "Carl's Doomsday Scenario", series = series, seq = "2"),
            book(7, "This Inevitable Ruin", series = series, seq = "7"),
            book(1, "Dungeon Crawler Carl", series = " $series ", seq = "1"),
            book(4, "The Gate of the Feral Gods", series = series, seq = "4"),
            book(6, "The Eye of the Bedlam Bride", series = series, seq = "6"),
            book(3, "The Dungeon Anarchist's Cookbook", series = series, seq = "3")
        )

        assertEquals(
            listOf(2, 1, 5, 3),
            novels.sortedBy { it.title }.take(4).map { it.id }
        )

        val groups = groupBySeries(
            novels + listOf(
                book(25, "The Novella", series = series, seq = "2.5"),
                book(100, "Standalone"),
                book(101, "Blank series", series = "   "),
                book(102, "Another series", series = "alpha"),
                book(103, "Last series", series = "Zulu")
            )
        )

        assertEquals(listOf("alpha", series, "Zulu"), groups.map { it.name })
        assertEquals(
            listOf(1, 2, 25, 3, 4, 5, 6, 7),
            groups.single { it.name == series }.books.map { it.id }
        )
        assertFalse(groups.flatMap { it.books }.any { it.id == 100 || it.id == 101 })
    }

    @Test
    fun groupBySeriesBreaksSequenceTiesByTitleIgnoringCase() {
        val groups = groupBySeries(
            listOf(
                book(1, "Zulu", series = "Series", seq = "2"),
                book(2, "alpha", series = "Series", seq = "2"),
                book(3, "Missing sequence", series = "Series"),
                book(4, "Invalid sequence", series = "Series", seq = "x")
            )
        )

        assertEquals(listOf(2, 1, 4, 3), groups.single().books.map { it.id })
    }

    @Test
    fun groupByAuthorKeepsCoauthorsTogetherAndUnknownAuthorsLast() {
        val groups = groupByAuthor(
            listOf(
                book(1, "Null author", author = null),
                book(2, "Blank author", author = "  "),
                book(3, "Collaboration one", author = "A and B"),
                book(4, "Collaboration two", author = " A and B "),
                book(5, "Zoe's book", author = "Zoe"),
                book(6, "Beta's book", author = "beta"),
                book(7, "Ada's book", author = "Ada")
            )
        )

        assertEquals(
            listOf("A and B", "Ada", "beta", "Zoe", UNKNOWN_AUTHOR),
            groups.map { it.name }
        )
        assertEquals(
            setOf(1, 2),
            groups.last().books.map { it.id }.toSet()
        )
        assertEquals(
            setOf(3, 4),
            groups.single { it.name == "A and B" }.books.map { it.id }.toSet()
        )
        assertFalse(groups.any { it.name == "A" || it.name == "B" })
    }

    @Test
    fun groupByAuthorOrdersSeriesBeforeStandaloneBooks() {
        val groups = groupByAuthor(
            listOf(
                book(10, "A standalone", author = "Ada"),
                book(11, "Second", author = " Ada ", series = " Alpha ", seq = "2"),
                book(12, "First", author = "Ada", series = "alpha", seq = "1"),
                book(13, "Other series", author = "Ada", series = "Zeta", seq = "1"),
                book(14, "b standalone", author = "Ada", series = " ", seq = "0")
            )
        )

        assertEquals("Ada", groups.single().name)
        assertEquals(listOf(12, 11, 13, 10, 14), groups.single().books.map { it.id })
    }

    @Test
    fun parseAddedAtSupportsUtcLocalTimestampsAndOffsets() {
        val fractional = Instant.parse("2026-10-29T05:37:29.837823Z")

        assertEquals(fractional, parseAddedAt("2026-10-29T05:37:29.837823"))
        assertEquals(fractional, parseAddedAt("2026-10-29 05:37:29.837823"))
        assertEquals(
            Instant.parse("2026-02-11T10:00:00Z"),
            parseAddedAt("2026-02-11T10:00:00")
        )
        assertEquals(fractional, parseAddedAt("2026-10-29T05:37:29.837823Z"))
        assertEquals(
            fractional,
            parseAddedAt("2026-10-29T07:37:29.837823+02:00")
        )
        assertEquals(
            fractional,
            parseAddedAt("2026-10-28T22:37:29.837823-07:00")
        )
        assertNull(parseAddedAt("garbage"))
        assertNull(parseAddedAt("2026-02-30T10:00:00"))
    }

    @Test
    fun recentlyAddedFiltersInvalidAndOldDatesAndSortsNewestFirst() {
        val now = Instant.parse("2026-10-29T15:00:00Z")
        val books = listOf(
            book(1, "October first", addedAt = "2026-10-01T00:00:00"),
            book(2, "Too old", addedAt = "2026-09-28T00:00:00"),
            book(3, "Invalid", addedAt = "garbage"),
            book(4, "Newest", addedAt = "2026-10-29 05:37:29.837823")
        )

        assertEquals(listOf(4, 1), recentlyAdded(books, now).map { it.id })
    }

    @Test
    fun recentlyAddedIncludesCutoffAndBreaksTiesByTitle() {
        val now = Instant.parse("2026-10-29T15:00:00Z")
        val books = listOf(
            book(1, "Zulu", addedAt = "2026-10-28T15:00:00"),
            book(2, "Alpha", addedAt = "2026-10-28T15:00:00Z"),
            book(3, "Before cutoff", addedAt = "2026-10-28T14:59:59.999999")
        )

        assertEquals(
            listOf(2, 1),
            recentlyAdded(books, now, days = 1).map { it.id }
        )
    }

    @Test
    fun groupBookLabelHandlesSeriesAndAuthorViews() {
        val numbered = book(1, "Numbered", series = "Series", seq = "2.5")
        val unnumbered = book(2, "Unnumbered", series = "Series")
        val standalone = book(3, "Standalone")
        val blankSeries = book(4, "Blank", series = "  ", seq = "3")

        assertEquals("#2.5", groupBookLabel(numbered, bySeries = true))
        assertNull(groupBookLabel(unnumbered, bySeries = true))
        assertNull(groupBookLabel(standalone, bySeries = true))
        assertEquals("#3", groupBookLabel(blankSeries, bySeries = true))

        assertEquals("Series #2.5", groupBookLabel(numbered, bySeries = false))
        assertEquals("Series", groupBookLabel(unnumbered, bySeries = false))
        assertNull(groupBookLabel(standalone, bySeries = false))
        assertNull(groupBookLabel(blankSeries, bySeries = false))
    }
}
