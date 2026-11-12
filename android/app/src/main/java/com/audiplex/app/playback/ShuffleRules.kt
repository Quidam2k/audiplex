package com.audiplex.app.playback

/**
 * #5881: shuffle turned on mid-play (Android Auto, Bluetooth, lock screen, or our own
 * fallback) plays the server's spaced order (#5867, POST /api/music/shuffle-order), not
 * ExoPlayer's plain random. Pure so the JVM tests pin it. Any mismatch is null, and null
 * means: leave ExoPlayer's random order alone.
 */
object ShuffleRules {
    const val TRACK_PREFIX = "track:"
    const val MIN_ITEMS = 3

    /** Track ids of the player's queue, in queue order; null if any item is not a plain track. */
    fun trackIds(mediaIds: List<String?>): List<Int>? {
        if (mediaIds.size < MIN_ITEMS) return null
        val ids = mediaIds.map { it?.takeIf { m -> m.startsWith(TRACK_PREFIX) }?.removePrefix(TRACK_PREFIX)?.toIntOrNull() }
        return if (ids.any { it == null }) null else ids.filterNotNull()
    }

    /**
     * Shuffled queue indices: the current item first (it keeps playing), then the rest in
     * the server's order. Null when the reply is not exactly the queue's ids, or a track repeats.
     */
    fun orderFromCurrent(itemIds: List<Int>, currentIndex: Int, serverOrder: List<Int>): IntArray? {
        if (currentIndex !in itemIds.indices) return null
        if (itemIds.toSet().size != itemIds.size) return null
        if (serverOrder.size != itemIds.size || serverOrder.toSet() != itemIds.toSet()) return null
        val indexOf = itemIds.withIndex().associate { (i, id) -> id to i }
        val current = itemIds[currentIndex]
        val rest = serverOrder.filter { it != current }.map { indexOf.getValue(it) }
        return (listOf(currentIndex) + rest).toIntArray()
    }
}
