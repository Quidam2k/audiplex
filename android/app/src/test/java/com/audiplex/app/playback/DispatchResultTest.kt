package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The command outcomes the device reports back (#900 Phase 3a).
 *
 * Before the ack existed, every one of these situations produced the same
 * thing: nothing. A command with unresolvable track ids, a malformed payload,
 * or a type this build has never heard of was consumed by the poll that
 * delivered it and then silently dropped — indistinguishable from a command
 * still in flight. That is precisely what made 2026-08-14 unexplainable, so
 * each failure shape is pinned here with the wording the DJ will read.
 */
class DispatchResultTest {

    @Test
    fun `nothing resolved is a failure, not a quiet success`() {
        val result = noTracks(listOf(65, 55))
        assertEquals("failed", result.status)
        assertEquals("dropped 2 unresolvable id(s): [65, 55]", result.detail)
    }

    @Test
    fun `a fully resolved command is a clean ok`() {
        val result = partialOrOk(listOf(65, 55), listOf(65, 55))
        assertEquals("ok", result.status)
        assertEquals("", result.detail)
    }

    @Test
    fun `a partly resolved command is ok but says what was lost`() {
        val result = partialOrOk(listOf(65, 55, 12), listOf(65, 55))
        assertEquals("ok", result.status)
        assertEquals("dropped 1 unresolvable id(s): [12]", result.detail)
    }

    @Test
    fun `a missing payload field names the field`() {
        assertEquals("bad_payload", badPayload("position_ms").status)
        assertEquals("missing position_ms", badPayload("position_ms").detail)
    }

    @Test
    fun `ok is the only status that marks a command acked server-side`() {
        // Mirrors PlaybackBus.ack: anything but "ok" lands as failed. Kept as a
        // test so the two vocabularies cannot drift apart silently.
        val failures = listOf(
            noTracks(listOf(1)).status,
            badPayload("url").status,
            DispatchResult("unknown_type", "wobble").status,
            DispatchResult("error", "boom").status,
        )
        failures.forEach { assertEquals(false, it == "ok") }
        assertEquals("ok", partialOrOk(listOf(1), listOf(1)).status)
    }

    @Test
    fun `#ride0928 dropped ids are named in order and never reordered`() {
        val result = partialOrOk(listOf(12, 5, 34, 6), listOf(5, 6))
        assertEquals("ok", result.status)
        assertEquals("dropped 2 unresolvable id(s): [12, 34]", result.detail)
        assertEquals(listOf(12, 34), droppedIds(listOf(12, 5, 34, 6), listOf(6, 5)))
    }

    @Test
    fun `#ride0928 resolveConcurrently with a throwing fake api feeds the ack detail`() = kotlinx.coroutines.runBlocking {
        val requested = listOf(1, 12, 3, 34)
        val resolved = resolveConcurrently(requested) { id ->
            if (id == 12) throw RuntimeException("404") else if (id == 34) null else id
        }
        val result = partialOrOk(requested, resolved)
        assertEquals("ok", result.status)
        assertEquals("dropped 2 unresolvable id(s): [12, 34]", result.detail)
    }
}
