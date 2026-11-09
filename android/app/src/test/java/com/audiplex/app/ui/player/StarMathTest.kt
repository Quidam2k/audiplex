package com.audiplex.app.ui.player

import com.audiplex.app.data.api.TrackRatingSchema
import com.squareup.moshi.Moshi
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** #6117: half-star taps, drawing, tap-to-clear, and wire compatibility. */
class StarMathTest {
    @Test fun tapSetsWholeStarAgainAddsHalfAgainGoesBack() {  // #5681
        assertEquals(4.0, tapStar(null, 4), 0.0)
        assertEquals(4.5, tapStar(4.0, 4), 0.0)
        assertEquals(4.0, tapStar(4.5, 4), 0.0)
        assertEquals(3.0, tapStar(4.5, 3), 0.0)
        assertEquals(5.0, tapStar(5.0, 5), 0.0)  // no 5.5
        assertEquals(5.0, tapStar(4.5, 5), 0.0)
    }

    @Test fun noTapEverWipesAnExistingRating() {  // #5681 Karen: a 4.5 can't be cleared by tapping
        for (current in listOf(0.5, 1.0, 3.5, 4.0, 4.5, 5.0)) for (star in 1..5) {
            assertEquals(true, tapStar(current, star) >= 1.0)
        }
    }

    @Test fun fourAndAHalfDrawsFourFullOneHalf() {
        assertEquals(
            listOf(StarFill.FULL, StarFill.FULL, StarFill.FULL, StarFill.FULL, StarFill.HALF),
            (1..5).map { starFill(it, 4.5) },
        )
        assertEquals(List(5) { StarFill.EMPTY }, (1..5).map { starFill(it, 0.0) })
    }

    @Test fun serverRowWithStarsParsesAndOldRowWithoutStarsStillParses() {
        val adapter = Moshi.Builder().build().adapter(TrackRatingSchema::class.java)
        val withStars = adapter.fromJson(
            """{"id":1,"track_id":3535,"rating":4,"stars":4.5,"note":"","updated_at":"x"}"""
        )!!
        assertEquals(4.5, withStars.stars!!, 0.0)
        assertEquals(4, withStars.rating)
        val old = adapter.fromJson(
            """{"id":2,"track_id":7,"rating":5,"note":"","updated_at":"x"}"""
        )!!
        assertNull(old.stars)
        assertEquals(5, old.rating)
    }
}
