package com.audiplex.app.playback

import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.CodingErrorAction

// Ticket #5969: record "what the phone was doing" in the process state summary so
// an overnight SIGKILL can be explained on the next start via ApplicationExitInfo.
// Pure Kotlin on purpose (no android.* imports) so it runs in plain JVM unit tests.

/** Android stores at most this many bytes in ActivityManager.setProcessStateSummary (#5969). */
internal const val STATE_SUMMARY_MAX_BYTES = 128

/** Callers write a snapshot on change, plus a heartbeat at this interval in seconds (#5969). */
internal const val SNAPSHOT_HEARTBEAT_S = 300L

private const val FIELD_MAX_CHARS = 12
private const val FOREGROUND_SERVICE_IMPORTANCE = 125
private const val SIGKILL = 9

/** A point-in-time description of what the phone and playback were doing (#5969). */
internal data class PhoneSnapshot(
    val atEpochS: Long,        // wall clock, seconds
    val play: String,          // "book" | "music" | "stream" | "bed" | "idle" (free text; sanitised)
    val bed: String,           // BedState name lowercased, e.g. "off","playing","recovering","stopped"
    val batteryPct: Int,       // -1 unknown
    val charging: Boolean?,    // null unknown
    val deviceIdle: Boolean?,  // Doze
    val powerSave: Boolean?,
    val standbyBucket: Int,    // -1 unknown
    val thermal: Int,          // -1 unknown (PowerManager thermal status 0..6)
)

private fun sanitiseField(raw: String): String {
    val out = StringBuilder()
    for (c in raw.lowercase()) {
        if (out.length >= FIELD_MAX_CHARS) break
        if ((c in 'a'..'z') || (c in '0'..'9') || c == '_') out.append(c)
    }
    return out.toString()
}

private fun Boolean?.flag(): String = when (this) {
    true -> "1"
    false -> "0"
    null -> "?"
}

private fun utf8Size(s: String): Int = s.toByteArray(Charsets.UTF_8).size

/**
 * Encodes this snapshot as a compact, version-tagged pipe string of at most
 * [STATE_SUMMARY_MAX_BYTES] UTF-8 bytes. If too long, whole `|k=v` pairs are
 * dropped from the end; a pair is never cut in the middle.
 */
internal fun PhoneSnapshot.encode(): String {
    val parts = arrayListOf(
        "v1",
        "t=$atEpochS",
        "p=${sanitiseField(play)}",
        "b=${sanitiseField(bed)}",
        "bat=$batteryPct",
        "chg=${charging.flag()}",
        "idle=${deviceIdle.flag()}",
        "ps=${powerSave.flag()}",
        "bkt=$standbyBucket",
        "thm=$thermal",
    )
    var joined = parts.joinToString("|")
    while (parts.size > 1 && utf8Size(joined) > STATE_SUMMARY_MAX_BYTES) {
        parts.removeAt(parts.size - 1)
        joined = parts.joinToString("|")
    }
    return joined
}

/**
 * True when this snapshot equals [other] in every field except [PhoneSnapshot.atEpochS].
 * A null [other] is never the same state.
 */
internal fun PhoneSnapshot.sameStateAs(other: PhoneSnapshot?): Boolean =
    other != null && this.copy(atEpochS = other.atEpochS) == other

/**
 * Inverse of [encode], for the next-start reader. Returns the k=v pairs plus "v" -> "1",
 * or null if the input is null/empty, not UTF-8 text starting with "v1|", or lacks a "t" key.
 * Unknown keys are kept; pairs without '=' are skipped. Never throws.
 */
internal fun decodeSnapshot(bytes: ByteArray?): Map<String, String>? {
    if (bytes == null || bytes.isEmpty()) return null
    return try {
        val text = Charsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(bytes))
            .toString()
        if (!text.startsWith("v1|")) return null
        val map = LinkedHashMap<String, String>()
        for (pair in text.split('|').drop(1)) {
            val eq = pair.indexOf('=')
            if (eq <= 0) continue
            map[pair.substring(0, eq)] = pair.substring(eq + 1)
        }
        if (!map.containsKey("t")) return null
        map["v"] = "1"
        map
    } catch (e: CharacterCodingException) {
        null
    } catch (e: RuntimeException) {
        null
    }
}

/** Maps an ActivityManager importance value to its constant name, or "IMPORTANCE_<n>". */
internal fun importanceName(importance: Int): String = when (importance) {
    100 -> "FOREGROUND"
    125 -> "FOREGROUND_SERVICE"
    200 -> "VISIBLE"
    230 -> "PERCEPTIBLE"
    325 -> "TOP_SLEEPING"
    350 -> "CANT_SAVE_STATE"
    300 -> "SERVICE"
    400 -> "CACHED"
    1000 -> "GONE"
    else -> "IMPORTANCE_$importance"
}

private fun statePhrase(state: Map<String, String>?): String {
    if (state == null) return "(no state recorded)"
    val p = state["p"]
    val sb = StringBuilder()
    sb.append(
        when {
            p == null -> "while play state unknown"
            p == "idle" -> "while idle"
            else -> "while playing $p"
        }
    )
    when (state["chg"]) {
        "0" -> sb.append(", on battery")
        "1" -> sb.append(", charging")
    }
    if (state["idle"] == "1") sb.append(", in Doze")
    if (state["ps"] == "1") sb.append(", battery saver on")
    return sb.toString()
}

/**
 * Produces one plain-English guess about why the process died. The line always starts
 * with "inference: " because it is a guess, not a fact. First matching rule wins (#5969).
 */
internal fun killHint(
    reason: String,
    status: Int,
    importance: Int,
    description: String,
    state: Map<String, String>?,
    pssKb: Long?,
): String {
    val prefix = "inference: "
    val body = when {
        description.contains("PackageUpdate") || description.contains("installPackage") ->
            "app was updated; not a real kill"
        reason == "USER_STOPPED" || reason == "USER_REQUESTED" ->
            "stopped by the user (force stop / swipe from settings)"
        reason == "LOW_MEMORY" ->
            "Android killed us for low memory"
        reason == "CRASH" || reason == "CRASH_NATIVE" || reason == "ANR" ->
            "our app crashed ($reason); see trace"
        reason == "EXCESSIVE_RESOURCE_USAGE" ->
            "Android killed us for using too much CPU or wakeups in the background"
        reason == "SIGNALED" && status == SIGKILL -> {
            val phrase = statePhrase(state)
            if (importance <= FOREGROUND_SERVICE_IMPORTANCE) {
                val held = if (pssKb != null) " (we held ${pssKb / 1024} MB)" else ""
                "the OS force-killed a foreground service $phrase: most likely memory reclaim " +
                    "(lmkd) or the phone's own app killer$held"
            } else {
                "the OS killed us in the background $phrase: normal reclaim of a cached app"
            }
        }
        else -> "no rule for $reason status $status"
    }
    return prefix + body
}

/**
 * #5969: the snapshot's play label. A bed running under a paused book reads "bed": that is the
 * overnight case the record exists for. Every label fits the 12-char sanitised field.
 */
internal fun playLabel(playing: Boolean, kind: PlayerKind?, bed: BedState): String {
    val short = when (kind) {
        PlayerKind.Audiobook -> "book"
        PlayerKind.Music -> "music"
        PlayerKind.Stream -> "stream"
        null -> null
    }
    return when {
        playing && short != null -> short
        bed == BedState.Playing || bed == BedState.Recovering -> "bed"
        short != null -> "paused_$short".take(12)
        else -> "idle"
    }
}

/** #5969: setProcessStateSummary is API 30+; older phones skip the snapshot and every other exit field still ships. */
internal const val STATE_SUMMARY_MIN_SDK = 30

/** Writes [encoded] through [write] when [sdkInt] supports it. True only when a write happened; never throws. */
internal fun writeStateSummary(sdkInt: Int, encoded: String, write: (ByteArray) -> Unit): Boolean {
    if (sdkInt < STATE_SUMMARY_MIN_SDK) return false
    return runCatching { write(encoded.toByteArray(Charsets.UTF_8)) }.isSuccess
}
