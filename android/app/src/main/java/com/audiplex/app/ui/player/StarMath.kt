package com.audiplex.app.ui.player

/**
 * Half-star rating arithmetic (#6117), kept out of the composable so it can be
 * unit-tested. Todd rates in halves out loud ("four and a half"), so the
 * now-playing stars take halves too: the left half of a star is n - 0.5, the
 * right half is n.
 */
internal enum class StarFill { FULL, HALF, EMPTY }

/**
 * #5681 (Todd: tapping star 4 gave 4.5 while driving): no half-star hit zones any more.
 * A tap on star N sets N. Tapping N again while the rating is N adds a half (4 -> 4.5),
 * and a third tap goes back to N. A tap never clears, so an existing 4.5 can't be wiped
 * by accident; long-press clears.
 */
internal fun tapStar(current: Double?, star: Int): Double =
    if (current == star.toDouble() && star < 5) star + 0.5 else star.toDouble()

/** How star [star] (1-5) is drawn for a [rating] in halves (0 = unrated). */
internal fun starFill(star: Int, rating: Double): StarFill = when {
    rating >= star -> StarFill.FULL
    rating >= star - 0.5 -> StarFill.HALF
    else -> StarFill.EMPTY
}
