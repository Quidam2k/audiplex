package com.audiplex.app.playback

import kotlin.math.PI
import kotlin.math.sin
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ClipEdgeFadeTest {
    private val delta = 0.001f

    @Test
    fun `fade timings match the specified durations`() {
        assertEquals(2500L, ClipEdgeFade.OUT_MS)
        assertEquals(1500L, ClipEdgeFade.IN_MS)
    }

    @Test
    fun `only djclip media ids are clips`() {
        assertTrue(ClipEdgeFade.isClip("djclip:123"))
        assertFalse(ClipEdgeFade.isClip("42"))
        assertFalse(ClipEdgeFade.isClip(null))
    }

    @Test
    fun `song without clip neighbours stays at full level`() {
        for (positionMs in listOf(0L, 100_000L, 199_900L)) {
            assertEquals(
                1f,
                ClipEdgeFade.level(false, false, false, positionMs, 200_000L),
                delta
            )
        }
    }

    @Test
    fun `song before a clip fades out during its final 2500 ms`() {
        assertEquals(
            1f,
            ClipEdgeFade.level(false, false, true, 190_000L, 200_000L),
            delta
        )
        assertEquals(
            0.7071068f,
            ClipEdgeFade.level(false, false, true, 198_750L, 200_000L),
            delta
        )
        assertEquals(
            0f,
            ClipEdgeFade.level(false, false, true, 200_000L, 200_000L),
            delta
        )
        assertEquals(
            1f,
            ClipEdgeFade.level(false, false, true, 197_500L, 200_000L),
            delta
        )
    }

    @Test
    fun `song after a clip fades in during its first 1500 ms`() {
        assertEquals(
            0f,
            ClipEdgeFade.level(true, false, false, 0L, 200_000L),
            delta
        )
        assertEquals(
            0.7071068f,
            ClipEdgeFade.level(true, false, false, 750L, 200_000L),
            delta
        )
        for (positionMs in listOf(1500L, 10_000L)) {
            assertEquals(
                1f,
                ClipEdgeFade.level(true, false, false, positionMs, 200_000L),
                delta
            )
        }
        assertEquals(
            0f,
            ClipEdgeFade.level(true, false, false, -100L, 200_000L),
            delta
        )
    }

    @Test
    fun `a clip stays at full level after its first moments regardless of its neighbours`() {
        for (prevIsClip in listOf(false, true)) {
            for (nextIsClip in listOf(false, true)) {
                for (positionMs in listOf(ClipEdgeFade.CLIP_IN_MS, 750L, 4999L, 5000L)) {
                    assertEquals(
                        1f,
                        ClipEdgeFade.level(
                            prevIsClip, true, nextIsClip, positionMs, 5000L
                        ),
                        delta
                    )
                }
            }
        }
    }

    @Test
    fun `short song between clips takes the smaller fade level`() {
        assertEquals(
            sin(PI / 2 * (250.0 / 1500)).toFloat(),
            ClipEdgeFade.level(true, false, true, 250L, 2000L),
            delta
        )
        assertEquals(
            sin(PI / 2 * (750.0 / 2500)).toFloat(),
            ClipEdgeFade.level(true, false, true, 1250L, 2000L),
            delta
        )
    }

    @Test
    fun `unknown duration disables fade out while preserving fade in`() {
        for (durationMs in listOf(-1L, 0L)) {
            for (positionMs in listOf(0L, 750L, 200_000L)) {
                assertEquals(
                    1f,
                    ClipEdgeFade.level(false, false, true, positionMs, durationMs),
                    delta
                )
            }
            assertEquals(
                0.7071068f,
                ClipEdgeFade.level(true, false, true, 750L, durationMs),
                delta
            )
        }
    }

}
