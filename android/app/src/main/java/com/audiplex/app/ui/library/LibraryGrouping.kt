package com.audiplex.app.ui.library

import com.audiplex.app.data.api.BookSummary
import java.time.Instant
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.temporal.ChronoUnit

data class BookGroup(val name: String, val books: List<BookSummary>)

const val UNKNOWN_AUTHOR = "Unknown author"

fun seqKey(seq: String?): Double =
    seq?.trim()?.toDoubleOrNull() ?: Double.POSITIVE_INFINITY

fun groupBySeries(books: List<BookSummary>): List<BookGroup> {
    val bookOrder = compareBy<BookSummary> { seqKey(it.seriesSequence) }
        .thenBy(String.CASE_INSENSITIVE_ORDER) { it.title }

    return books
        .filter { !it.series.isNullOrBlank() }
        .groupBy { it.series.orEmpty().trim() }
        .map { (name, members) ->
            BookGroup(name, members.sortedWith(bookOrder))
        }
        .sortedWith(compareBy(String.CASE_INSENSITIVE_ORDER) { it.name })
}

fun groupByAuthor(books: List<BookSummary>): List<BookGroup> {
    val bookOrder = compareBy<BookSummary> { it.series.isNullOrBlank() }
        .thenBy(String.CASE_INSENSITIVE_ORDER) { it.series.orEmpty().trim() }
        .thenBy {
            if (it.series.isNullOrBlank()) {
                Double.POSITIVE_INFINITY
            } else {
                seqKey(it.seriesSequence)
            }
        }
        .thenBy(String.CASE_INSENSITIVE_ORDER) { it.title }

    return books
        .groupBy { it.author?.trim()?.takeIf { name -> name.isNotBlank() } ?: UNKNOWN_AUTHOR }
        .map { (name, members) ->
            BookGroup(name, members.sortedWith(bookOrder))
        }
        .sortedWith(
            compareBy<BookGroup> { it.name == UNKNOWN_AUTHOR }
                .thenBy(String.CASE_INSENSITIVE_ORDER) { it.name }
        )
}

fun parseAddedAt(s: String): Instant? {
    return try {
        val normalized = s.replaceFirst(' ', 'T')
        val timePart = normalized.substringAfter('T', "")
        val hasOffset = normalized.endsWith("Z", ignoreCase = true) ||
            timePart.any { it == '+' || it == '-' }

        if (hasOffset) {
            OffsetDateTime.parse(normalized).toInstant()
        } else {
            LocalDateTime.parse(normalized).toInstant(ZoneOffset.UTC)
        }
    } catch (_: Exception) {
        null
    }
}

fun recentlyAdded(
    books: List<BookSummary>,
    now: Instant = Instant.now(),
    days: Long = 30
): List<BookSummary> {
    val cutoff = now.minus(days, ChronoUnit.DAYS)

    return books
        .mapNotNull { book ->
            parseAddedAt(book.addedAt)?.let { addedAt -> book to addedAt }
        }
        .filter { (_, addedAt) -> addedAt >= cutoff }
        .sortedWith(
            compareByDescending<Pair<BookSummary, Instant>> { it.second }
                .thenBy { it.first.title }
        )
        .map { it.first }
}

fun groupBookLabel(book: BookSummary, bySeries: Boolean): String? {
    if (bySeries) {
        return book.seriesSequence?.let { "#$it" }
    }

    val series = book.series
    return if (!series.isNullOrBlank()) {
        series + (book.seriesSequence?.let { " #$it" } ?: "")
    } else {
        null
    }
}
