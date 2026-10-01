package com.audiplex.app.playback

import com.audiplex.app.data.api.ResumeSnapshotDto
import org.junit.Assert.assertEquals
import org.junit.Test

/** #3601: which saved queues come back on their own after an app kill. */
class QueueRestoreTest {
    private fun snap(origin: String?, age: Double = 60.0, index: Int = 1) = ResumeSnapshotDto(
        trackIds = listOf(10, 11, -3, 12), index = index, positionMs = 61_000L,
        origin = origin, ageSeconds = age,
    )

    @Test fun djQueueComesBackFromTheSavedSong() =
        assertEquals(listOf(11, 12), idsToRestore(snap(ORIGIN_DJ), allowManual = false))

    @Test fun manualQueueStaysGoneByDefault() =
        assertEquals(emptyList<Int>(), idsToRestore(snap(ORIGIN_MANUAL), allowManual = false))

    @Test fun manualQueueComesBackWhenTheSettingIsOn() =
        assertEquals(listOf(11, 12), idsToRestore(snap(ORIGIN_MANUAL), allowManual = true))

    @Test fun unknownOriginNeverAutoRestores() =
        assertEquals(emptyList<Int>(), idsToRestore(snap(null), allowManual = true))

    @Test fun aDayOldQueueIsHistory() =
        assertEquals(emptyList<Int>(), idsToRestore(snap(ORIGIN_DJ, age = 25 * 3600.0), allowManual = false))
}
