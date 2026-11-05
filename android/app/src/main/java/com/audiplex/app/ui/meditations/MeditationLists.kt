package com.audiplex.app.ui.meditations

import com.audiplex.app.data.api.BookSummary
import com.audiplex.app.data.db.DownloadEntity

/** #845, #5465: pure decisions for the Meditations list, kept here so they can be unit-tested. */
object MeditationLists {

    /**
     * #845: whether "Download all" should queue this row. A copy that is on the phone is left alone
     * (re-queueing deletes and re-fetches it, which is the flicker); so is a download whose work is
     * really running. Everything else is queued: never downloaded, FAILED, PAUSED, a COMPLETED row
     * whose file was deleted, and a PENDING/DOWNLOADING row with no live work behind it (left by a
     * crash or a killed worker).
     */
    fun shouldQueue(status: String?, filePresent: Boolean, workActive: Boolean): Boolean = when (status) {
        DownloadEntity.Status.COMPLETED -> !filePresent
        DownloadEntity.Status.PENDING, DownloadEntity.Status.DOWNLOADING -> !workActive
        else -> true
    }

    /**
     * #5465: newest first. added_at is the server's timestamp string, one format for every row, so it
     * sorts as text. A blank one goes last; equal times fall back to the title.
     */
    fun newestFirst(books: List<BookSummary>): List<BookSummary> =
        books.sortedWith(
            compareBy<BookSummary> { it.addedAt.isBlank() }
                .thenByDescending { it.addedAt }
                .thenBy { it.title.lowercase() }
        )

    /**
     * #5462: what the background auto-download may fetch. It needs the real bed list (no fallback),
     * so a sleep sound is never downloaded as a meditation.
     */
    fun autoCandidates(books: List<BookSummary>, bedIds: Set<Int>): List<BookSummary> =
        split(books, bedIds).meditations

    data class Sections(val meditations: List<BookSummary>, val sleepSounds: List<BookSummary>)

    /**
     * #5465: sleep sounds get their own section. The beds are the server's /api/sleep-beds list,
     * which it finds by title, so this never decides what the sleep bed plays. If that list could
     * not be fetched (null), nothing is hidden: every row stays under Meditations.
     */
    fun split(books: List<BookSummary>, bedIds: Set<Int>?): Sections {
        val sorted = newestFirst(books)
        if (bedIds == null) return Sections(sorted, emptyList())
        val (beds, rest) = sorted.partition { it.id in bedIds }
        return Sections(rest, beds)
    }
}
