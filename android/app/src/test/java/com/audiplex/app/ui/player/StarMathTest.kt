package com.audiplex.app.ui.player

import com.audiplex.app.data.api.TrackRatingSchema
import com.squareup.moshi.Moshi
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** #6117: half-star taps, drawing, tap-to-clear, and wire compatibility. */
class StarMathTest {
    @Test fun leftHalfIsAHalfStarRightHalfIsWhole() {
        assertEquals(4.5, starValue(5, leftHalf = true), 0.0)
        assertEquals(5.0, starValue(5, leftHalf = false), 0.0)
        assertEquals(0.5, starValue(1, leftHalf = true), 0.0)
    }

    @Test fun fourAndAHalfDrawsFourFullOneHalf() {
        assertEquals(
            listOf(StarFill.FULL, StarFill.FULL, StarFill.FULL, StarFill.FULL, StarFill.HALF),
            (1..5).map { starFill(it, 4.5) },
        )
        assertEquals(List(5) { StarFill.EMPTY }, (1..5).map { starFill(it, 0.0) })
    }

    @Test fun tappingTheCurrentValueClearsIt() {
        assertNull(nextRating(4.5, 4.5))
        assertEquals(4.0, nextRating(4.5, 4.0)!!, 0.0)
        assertEquals(4.5, nextRating(null, 4.5)!!, 0.0)
        // The other half of the same star changes the value, it doesn't clear.
        assertEquals(5.0, nextRating(4.5, 5.0)!!, 0.0)
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
