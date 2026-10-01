package com.audiplex.app.ui.player

/**
 * Half-star rating arithmetic (#6117), kept out of the composable so it can be
 * unit-tested. Todd rates in halves out loud ("four and a half"), so the
 * now-playing stars take halves too: the left half of a star is n - 0.5, the
 * right half is n.
 */
internal enum class StarFill { FULL, HALF, EMPTY }

/** The value a tap on star [star] (1-5) means, by which half was hit. */
internal fun starValue(star: Int, leftHalf: Boolean): Double =
    if (leftHalf) star - 0.5 else star.toDouble()

/** How star [star] (1-5) is drawn for a [rating] in halves (0 = unrated). */
internal fun starFill(star: Int, rating: Double): StarFill = when {
    rating >= star -> StarFill.FULL
    rating >= star - 0.5 -> StarFill.HALF
    else -> StarFill.EMPTY
}

/** The rating after a tap: tapping the value already set clears it (null). */
internal fun nextRating(current: Double?, tapped: Double): Double? =
    if (current == tapped) null else tapped
