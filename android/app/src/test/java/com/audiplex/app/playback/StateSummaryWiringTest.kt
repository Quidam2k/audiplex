package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** #5969: the play label and the API-30 guard around setProcessStateSummary. */
class StateSummaryWiringTest {

    @Test
    fun `a bed under a paused book reads bed`() {
        assertEquals("bed", playLabel(false, PlayerKind.Audiobook, BedState.Playing))
        assertEquals("bed", playLabel(false, null, BedState.Recovering))
    }

    @Test
    fun `playing wins over the bed and paused names its kind`() {
        assertEquals("music", playLabel(true, PlayerKind.Music, BedState.Playing))
        assertEquals("paused_book", playLabel(false, PlayerKind.Audiobook, BedState.Off))
        assertEquals("idle", playLabel(false, null, BedState.Off))
        assertEquals("idle", playLabel(true, null, BedState.Stopped))
    }

    @Test
    fun `every label survives the 12-char field`() {
        for (playing in listOf(true, false)) for (kind in PlayerKind.values().toList() + null) for (bed in BedState.values()) {
            val label = playLabel(playing, kind, bed)
            assertTrue(label, label.length <= 12 && label.all { it in 'a'..'z' || it == '_' })
        }
    }

    @Test
    fun `below API 30 nothing is written`() {
        var wrote = false
        assertFalse(writeStateSummary(29, "v1|t=1") { wrote = true })
        assertFalse(wrote)
    }

    @Test
    fun `API 30 writes the bytes and a failing write is swallowed`() {
        var got: ByteArray? = null
        assertTrue(writeStateSummary(30, "v1|t=1") { got = it })
        assertEquals("v1|t=1", got!!.toString(Charsets.UTF_8))
        assertFalse(writeStateSummary(34, "v1|t=1") { throw SecurityException("no") })
    }

    @Test
    fun `exit detail carries the extras but never lets them override a core key`() {
        val rec = ExitRecord(
            timestamp = 5L, reason = "SIGNALED", description = "SIGNALED", status = 9, importance = 125,
            unexpected = false, extra = mapOf("reason" to "spoofed", "state_at_death" to "t=1 p=bed", "hint" to "inference: x"),
        )
        val d = rec.detail()
        assertEquals("SIGNALED", d["reason"])
        assertEquals("SIGKILL", d["signal"])
        assertEquals("t=1 p=bed", d["state_at_death"])
        assertEquals("inference: x", d["hint"])
    }
}
