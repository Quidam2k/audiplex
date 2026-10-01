package com.audiplex.app.playback

import com.audiplex.app.data.api.ResumeSnapshotDto

// #3601: who built the current music queue, reported to the server so its
// resume snapshot knows whether the queue should come back on its own.
const val ORIGIN_DJ = "dj"
const val ORIGIN_MANUAL = "manual"

/** MediaMetadata extra carrying the origin, so a re-hydrated session keeps it. */
internal const val EXTRA_QUEUE_ORIGIN = "queueOrigin"

/** Older than this, a saved queue is history, not "where I was". */
internal const val RESTORE_MAX_AGE_SECONDS = 24 * 60 * 60.0

/** Tracks resolved before the paused queue appears; the rest follow behind. */
internal const val RESTORE_HEAD = 40

/**
 * The track ids to put back, from the saved song on, or empty when this
 * snapshot shouldn't come back by itself (#3601): too old, or a queue Todd
 * built himself while [allowManual] (the Settings toggle) is off. A snapshot
 * with no origin came from an app build that didn't report one, so it isn't
 * known to be the DJ's and stays put.
 */
internal fun idsToRestore(snap: ResumeSnapshotDto, allowManual: Boolean): List<Int> {
    if (snap.ageSeconds > RESTORE_MAX_AGE_SECONDS) return emptyList()
    val wanted = snap.origin == ORIGIN_DJ || (allowManual && snap.origin == ORIGIN_MANUAL)
    if (!wanted) return emptyList()
    return snap.trackIds.drop(snap.index.coerceAtLeast(0)).filter { it > 0 }
}
