package com.audiplex.app.playback

/**
 * #5684: the queue after removing [index], with the playing item's new index, or null
 * when [index] is out of range or IS the playing item (that one is never removed).
 */
internal fun <T> queueWithout(items: List<T>, currentIndex: Int, index: Int): Pair<List<T>, Int>? {
    if (index !in items.indices || index == currentIndex) return null
    val current = if (index < currentIndex) currentIndex - 1 else currentIndex
    return items.toMutableList().apply { removeAt(index) } to current
}

/** #5684: where a row dragged from [from] by [offsetPx] lands, rows [rowPx] tall. */
internal fun dragTarget(from: Int, offsetPx: Float, rowPx: Float, size: Int): Int {
    if (rowPx <= 0f || size <= 0) return from
    return (from + Math.round(offsetPx / rowPx)).coerceIn(0, size - 1)
}
