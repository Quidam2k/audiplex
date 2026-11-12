package com.audiplex.app.playback

import java.nio.charset.StandardCharsets.UTF_8
import java.util.Random
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins the #5969 state-at-death record.
 *
 * Android hands back at most 128 bytes from `setProcessStateSummary` on the next
 * `ApplicationExitInfo`. An over-long or unparsable summary must degrade to "no state"
 * and must never crash the exit reporter. These tests cover the encode/decode round trip,
 * the size cap, tolerant decoding, and the kill-hint inference rules.
 */
class PhoneStateSnapshotTest {

    private fun snapshot(
        atEpochS: Long = 1_700_000_000L,
        play: String = "playing",
        bed: String = "bed",
        batteryPct: Int = 87,
        charging: Boolean? = true,
        deviceIdle: Boolean? = false,
        powerSave: Boolean? = null,
        standbyBucket: Int = 10,
        thermal: Int = 0,
    ) = PhoneSnapshot(
        atEpochS = atEpochS,
        play = play,
        bed = bed,
        batteryPct = batteryPct,
        charging = charging,
        deviceIdle = deviceIdle,
        powerSave = powerSave,
        standbyBucket = standbyBucket,
        thermal = thermal,
    )

    private fun decode(text: String): Map<String, String>? = decodeSnapshot(text.toByteArray(UTF_8))

    // ---- encode / decode ----

    @Test
    fun `round trip encode then decode gives the expected map`() {
        val encoded = snapshot().encode()
        val decoded = decodeSnapshot(encoded.toByteArray(UTF_8))
        val expected = mapOf(
            "v" to "1",
            "t" to "1700000000",
            "p" to "playing",
            "b" to "bed",
            "bat" to "87",
            "chg" to "1",
            "idle" to "0",
            "ps" to "?",
            "bkt" to "10",
            "thm" to "0",
        )
        assertEquals(expected, decoded)
    }

    @Test
    fun `charging false encodes as zero`() {
        val decoded = decodeSnapshot(snapshot(charging = false).encode().toByteArray(UTF_8))
        assertNotNull(decoded)
        assertEquals("0", decoded!!["chg"])
    }

    @Test
    fun `null booleans encode as question mark`() {
        val encoded = snapshot(charging = null, deviceIdle = null, powerSave = null).encode()
        assertTrue(encoded, encoded.contains("chg=?"))
        assertTrue(encoded, encoded.contains("idle=?"))
        assertTrue(encoded, encoded.contains("ps=?"))
    }

    @Test
    fun `play and bed are sanitised`() {
        val encoded = snapshot(play = "Bed|x=1 MUSIC-very-long-name").encode()
        val segments = encoded.split('|')
        assertEquals(encoded, "v1", segments.first())
        for (pair in segments.drop(1)) {
            assertEquals(pair, 1, pair.count { it == '=' })
        }
        val decoded = decodeSnapshot(encoded.toByteArray(UTF_8))
        assertNotNull(decoded)
        val p = decoded!!["p"]
        assertNotNull(p)
        assertTrue(p, p!!.length <= 12)
        assertTrue(p, p.all { it in 'a'..'z' || it in '0'..'9' || it == '_' })
    }

    @Test
    fun `encoded size never exceeds the cap`() {
        val long = "a".repeat(50)
        val snapshots = listOf(
            snapshot(),
            snapshot(
                atEpochS = Long.MAX_VALUE,
                play = long,
                bed = long,
                batteryPct = Int.MIN_VALUE,
                charging = null,
                deviceIdle = null,
                powerSave = null,
                standbyBucket = Int.MIN_VALUE,
                thermal = Int.MIN_VALUE,
            ),
            snapshot(
                atEpochS = Long.MIN_VALUE,
                play = long,
                bed = long,
                batteryPct = Int.MAX_VALUE,
                standbyBucket = Int.MAX_VALUE,
                thermal = Int.MAX_VALUE,
            ),
            snapshot(play = "", bed = ""),
            snapshot(play = "é日本語🎵", bed = "\u0000\n"),
        )
        for (s in snapshots) {
            val encoded = s.encode()
            val size = encoded.toByteArray(UTF_8).size
            assertTrue("size $size for $encoded", size <= STATE_SUMMARY_MAX_BYTES)
            assertTrue(encoded, encoded.startsWith("v1|t="))
        }
    }

    // ---- decodeSnapshot tolerance ----

    @Test
    fun `decode returns null for unusable input`() {
        assertNull(decodeSnapshot(null))
        assertNull(decodeSnapshot(ByteArray(0)))
        assertNull(decode("garbage"))
        assertNull(decode("v2|t=1"))
        assertNull(decode("v1|p=bed"))
        assertNull(
            decodeSnapshot(
                byteArrayOf(0xFF.toByte(), 0xFE.toByte(), 0x00, 0xC0.toByte(), 0x80.toByte(), 0xFD.toByte()),
            ),
        )
    }

    @Test
    fun `decode never throws on arbitrary bytes`() {
        val random = Random(42)
        repeat(300) {
            val bytes = ByteArray(random.nextInt(200))
            random.nextBytes(bytes)
            decodeSnapshot(bytes)
            decodeSnapshot("v1|t=".toByteArray(UTF_8) + bytes)
            decodeSnapshot("v1|".toByteArray(UTF_8) + bytes)
        }
    }

    @Test
    fun `decode skips a malformed pair`() {
        val decoded = decode("v1|t=5|junk|p=bed")
        assertNotNull(decoded)
        assertEquals("5", decoded!!["t"])
        assertEquals("bed", decoded["p"])
        assertFalse(decoded.containsKey("junk"))
    }

    @Test
    fun `decode keeps unknown keys`() {
        val decoded = decode("v1|t=5|zz=9")
        assertNotNull(decoded)
        assertEquals("9", decoded!!["zz"])
        assertEquals("5", decoded["t"])
        assertEquals("1", decoded["v"])
    }

    // ---- sameStateAs ----

    @Test
    fun `sameStateAs ignores time`() {
        val a = snapshot(atEpochS = 100L)
        val b = snapshot(atEpochS = 999L)
        assertTrue(a.sameStateAs(b))
    }

    @Test
    fun `sameStateAs detects a play change`() {
        val a = snapshot(play = "playing")
        val b = snapshot(play = "paused")
        assertFalse(a.sameStateAs(b))
    }

    @Test
    fun `sameStateAs is false for null`() {
        assertFalse(snapshot().sameStateAs(null))
    }

    // ---- importanceName ----

    @Test
    fun `importanceName maps known and unknown values`() {
        assertEquals("FOREGROUND_SERVICE", importanceName(125))
        assertEquals("CACHED", importanceName(400))
        assertEquals("IMPORTANCE_12345", importanceName(12345))
    }

    // ---- killHint ----

    private val bedState = mapOf(
        "v" to "1",
        "t" to "1700000000",
        "p" to "bed",
        "chg" to "0",
        "idle" to "1",
    )

    @Test
    fun `killHint flags a package update`() {
        val byUpdate = killHint("OTHER", 0, 100, "stop pkg due to PackageUpdate", null, null)
        assertTrue(byUpdate, byUpdate.contains("updated"))
        val byInstall = killHint("OTHER", 0, 100, "installPackage", null, null)
        assertTrue(byInstall, byInstall.contains("updated"))
    }

    @Test
    fun `killHint flags user stop`() {
        val hint = killHint("USER_STOPPED", 0, 100, "", null, null)
        assertTrue(hint, hint.contains("user"))
    }

    @Test
    fun `killHint flags low memory`() {
        val hint = killHint("LOW_MEMORY", 0, 400, "", null, null)
        assertTrue(hint, hint.contains("low memory"))
    }

    @Test
    fun `killHint flags a crash`() {
        val hint = killHint("CRASH", 0, 100, "", null, null)
        assertTrue(hint, hint.contains("crashed"))
    }

    @Test
    fun `killHint describes a foreground service kill with state`() {
        val hint = killHint("SIGNALED", 9, 125, "", bedState, null)
        assertTrue(hint, hint.contains("foreground service"))
        assertTrue(hint, hint.contains("playing bed"))
        assertTrue(hint, hint.contains("on battery"))
        assertTrue(hint, hint.contains("in Doze"))
    }

    @Test
    fun `killHint includes memory when pss is known`() {
        val hint = killHint("SIGNALED", 9, 125, "", bedState, 204_800L)
        assertTrue(hint, hint.contains("200 MB"))
    }

    @Test
    fun `killHint describes a cached process kill as background`() {
        val hint = killHint("SIGNALED", 9, 400, "", null, null)
        assertTrue(hint, hint.contains("background"))
    }

    @Test
    fun `killHint says no state recorded when state is null`() {
        val hint = killHint("SIGNALED", 9, 125, "", null, null)
        assertTrue(hint, hint.contains("no state recorded"))
    }

    @Test
    fun `killHint falls back for an unknown reason`() {
        val hint = killHint("SOMETHING_NEW", 0, 100, "", null, null)
        assertTrue(hint, hint.contains("no rule"))
    }

    @Test
    fun `every killHint starts with the inference prefix`() {
        val hints = listOf(
            killHint("OTHER", 0, 100, "PackageUpdate", null, null),
            killHint("USER_STOPPED", 0, 100, "", null, null),
            killHint("LOW_MEMORY", 0, 400, "", null, null),
            killHint("CRASH", 0, 100, "", null, null),
            killHint("SIGNALED", 9, 125, "", bedState, 204_800L),
            killHint("SIGNALED", 9, 400, "", null, null),
            killHint("SIGNALED", 9, 125, "", null, null),
            killHint("SOMETHING_NEW", 0, 100, "", null, null),
            killHint("", 0, 0, "", emptyMap(), 0L),
        )
        for (hint in hints) {
            assertTrue(hint, hint.startsWith("inference: "))
        }
    }
}
