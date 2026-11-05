package com.audiplex.app.ui.meditations

import com.audiplex.app.data.api.BookSummary
import com.audiplex.app.data.db.DownloadEntity.Status
import org.junit.Assert.*
import org.junit.Test

/** Unit tests for the pure Meditations list decisions (#845, #5465). */
class MeditationListsTest {
    private val newer = "2026-11-04 14:12:47.357212"
    private val older = "2026-08-08 22:19:28.010433"

    private fun book(id: Int, title: String, addedAt: String) = BookSummary(
        id = id,
        title = title,
        author = null,
        narrator = null,
        series = null,
        seriesSequence = null,
        durationSeconds = 0.0,
        hasCover = false,
        addedAt = addedAt
    )

    private fun ids(books: List<BookSummary>) = books.map { it.id }

    @Test
    fun neverDownloadedQueues() {
        assertTrue(MeditationLists.shouldQueue(null, filePresent = false, workActive = false))
    }

    @Test
    fun completedWithFileIsSkipped() {
        assertFalse(MeditationLists.shouldQueue(Status.COMPLETED, filePresent = true, workActive = false))
    }

    @Test
    fun completedWithoutFileQueues() {
        assertTrue(MeditationLists.shouldQueue(Status.COMPLETED, filePresent = false, workActive = false))
    }

    @Test
    fun pendingAndDownloadingWithActiveWorkAreSkipped() {
        assertFalse(MeditationLists.shouldQueue(Status.PENDING, filePresent = false, workActive = true))
        assertFalse(MeditationLists.shouldQueue(Status.DOWNLOADING, filePresent = false, workActive = true))
    }

    @Test
    fun stalePendingAndDownloadingQueue() {
        assertTrue(MeditationLists.shouldQueue(Status.PENDING, filePresent = false, workActive = false))
        assertTrue(MeditationLists.shouldQueue(Status.DOWNLOADING, filePresent = false, workActive = false))
    }

    @Test
    fun failedQueuesEvenWithFileAndNoWork() {
        assertTrue(MeditationLists.shouldQueue(Status.FAILED, filePresent = true, workActive = false))
    }

    @Test
    fun pausedQueues() {
        assertTrue(MeditationLists.shouldQueue(Status.PAUSED, filePresent = false, workActive = false))
    }

    @Test
    fun newestFirstOrdersByAddedAtDescending() {
        val sorted = MeditationLists.newestFirst(listOf(book(1, "A", older), book(2, "B", newer)))
        assertEquals(listOf(2, 1), ids(sorted))
    }

    @Test
    fun blankAddedAtGoesLast() {
        val sorted = MeditationLists.newestFirst(listOf(book(1, "A", ""), book(2, "B", older), book(3, "C", newer)))
        assertEquals(listOf(3, 2, 1), ids(sorted))
    }

    @Test
    fun equalAddedAtTiesBreakByTitleIgnoringCase() {
        val sorted = MeditationLists.newestFirst(
            listOf(book(1, "banana", newer), book(2, "Cherry", newer), book(3, "Apple", newer))
        )
        assertEquals(listOf(3, 1, 2), ids(sorted))
    }

    @Test
    fun splitMovesBedsToSleepSoundsNewestFirst() {
        val books = listOf(
            book(1, "Rain", older),
            book(2, "Body Scan", older),
            book(3, "Ocean", newer),
            book(4, "Breath", newer)
        )
        val sections = MeditationLists.split(books, setOf(1, 3))
        assertEquals(listOf(4, 2), ids(sections.meditations))
        assertEquals(listOf(3, 1), ids(sections.sleepSounds))
    }

    @Test
    fun splitWithNullBedIdsKeepsEverythingInMeditations() {
        val sections = MeditationLists.split(listOf(book(1, "A", older), book(2, "B", newer)), null)
        assertEquals(listOf(2, 1), ids(sections.meditations))
        assertTrue(sections.sleepSounds.isEmpty())
    }

    @Test
    fun splitWithEmptyBedIdsKeepsEverythingInMeditations() {
        val sections = MeditationLists.split(listOf(book(1, "A", older), book(2, "B", newer)), emptySet())
        assertEquals(listOf(2, 1), ids(sections.meditations))
        assertTrue(sections.sleepSounds.isEmpty())
    }

    @Test
    fun autoCandidatesNeverIncludeSleepBeds() {  // #5462
        val books = listOf(book(1, "Rain", older), book(2, "Body Scan", newer), book(3, "Brown Noise", newer))
        assertEquals(listOf(2), ids(MeditationLists.autoCandidates(books, setOf(1, 3))))
    }
}
