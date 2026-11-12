package com.audiplex.app.playback

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

// #5881 mid-play shuffle plays the server's spaced order, current song first.
class ShuffleRulesTest {

    @Test
    fun trackIds_parsesPlainTracksInQueueOrder() {
        val ids = ShuffleRules.trackIds(listOf("track:5", "track:7", "track:9"))
        assertEquals(listOf(5, 7, 9), ids)
    }

    @Test
    fun trackIds_djClipMakesItNull() {
        assertNull(ShuffleRules.trackIds(listOf("track:5", "djclip:1", "track:9")))
    }

    @Test
    fun trackIds_streamItemMakesItNull() {
        assertNull(ShuffleRules.trackIds(listOf("track:5", "stream:http://x", "track:9")))
    }

    @Test
    fun trackIds_nullItemMakesItNull() {
        assertNull(ShuffleRules.trackIds(listOf("track:5", null, "track:9")))
    }

    @Test
    fun trackIds_nonNumericIdMakesItNull() {
        assertNull(ShuffleRules.trackIds(listOf("track:5", "track:abc", "track:9")))
    }

    @Test
    fun trackIds_fewerThanThreeItemsIsNull() {
        assertNull(ShuffleRules.trackIds(emptyList()))
        assertNull(ShuffleRules.trackIds(listOf("track:5")))
        assertNull(ShuffleRules.trackIds(listOf("track:5", "track:7")))
    }

    @Test
    fun orderFromCurrent_currentFirstThenServerOrder() {
        val result = ShuffleRules.orderFromCurrent(
            itemIds = listOf(10, 20, 30, 40),
            currentIndex = 2,
            serverOrder = listOf(40, 10, 30, 20),
        )
        assertArrayEquals(intArrayOf(2, 3, 0, 1), result)
    }

    @Test
    fun orderFromCurrent_currentAlreadyFirstInServerOrder() {
        val result = ShuffleRules.orderFromCurrent(
            itemIds = listOf(10, 20, 30),
            currentIndex = 0,
            serverOrder = listOf(10, 30, 20),
        )
        assertArrayEquals(intArrayOf(0, 2, 1), result)
    }

    @Test
    fun orderFromCurrent_serverReplyMissingAnIdIsNull() {
        assertNull(
            ShuffleRules.orderFromCurrent(
                itemIds = listOf(10, 20, 30),
                currentIndex = 0,
                serverOrder = listOf(10, 20, 99),
            )
        )
        assertNull(
            ShuffleRules.orderFromCurrent(
                itemIds = listOf(10, 20, 30),
                currentIndex = 0,
                serverOrder = listOf(10, 20, 20),
            )
        )
    }

    @Test
    fun orderFromCurrent_serverReplyWithExtraIdIsNull() {
        assertNull(
            ShuffleRules.orderFromCurrent(
                itemIds = listOf(10, 20, 30),
                currentIndex = 0,
                serverOrder = listOf(10, 20, 30, 99),
            )
        )
    }

    @Test
    fun orderFromCurrent_serverReplyOfDifferentLengthIsNull() {
        assertNull(
            ShuffleRules.orderFromCurrent(
                itemIds = listOf(10, 20, 30),
                currentIndex = 0,
                serverOrder = listOf(10, 20),
            )
        )
        assertNull(
            ShuffleRules.orderFromCurrent(
                itemIds = listOf(10, 20, 30),
                currentIndex = 0,
                serverOrder = emptyList(),
            )
        )
    }

    @Test
    fun orderFromCurrent_repeatedTrackInQueueIsNull() {
        assertNull(
            ShuffleRules.orderFromCurrent(
                itemIds = listOf(10, 20, 10),
                currentIndex = 0,
                serverOrder = listOf(10, 20, 10),
            )
        )
    }

    @Test
    fun orderFromCurrent_currentIndexOutOfRangeIsNull() {
        val itemIds = listOf(10, 20, 30)
        val serverOrder = listOf(30, 20, 10)
        assertNull(ShuffleRules.orderFromCurrent(itemIds, -1, serverOrder))
        assertNull(ShuffleRules.orderFromCurrent(itemIds, 3, serverOrder))
        assertNull(ShuffleRules.orderFromCurrent(itemIds, 4, serverOrder))
    }

    @Test
    fun orderFromCurrent_resultIsPermutationOfQueueIndices() {
        val n = 12
        val itemIds = (0 until n).map { 100 + it }
        val serverOrder = itemIds.reversed()
        val currentIndex = 5

        val result = ShuffleRules.orderFromCurrent(itemIds, currentIndex, serverOrder)

        assertEquals(n, result!!.size)
        assertEquals((0 until n).toList(), result.sorted())
        assertEquals(currentIndex, result[0])
        assertArrayEquals(
            intArrayOf(5, 11, 10, 9, 8, 7, 6, 4, 3, 2, 1, 0),
            result,
        )
    }
}
