package com.audiplex.app.playback

import android.app.ActivityManager
import android.app.ApplicationExitInfo
import android.content.Context
import android.os.Build
import android.util.Log
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.data.api.ClientLogDto
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import javax.inject.Inject
import javax.inject.Singleton

/** One process death, flattened off ApplicationExitInfo so it can be tested. */
internal data class ExitRecord(
    val timestamp: Long,
    val reason: String,
    val description: String,
    val status: Int,
    val importance: Int,
    val unexpected: Boolean,
) {
    fun detail(): Map<String, String> = mapOf(
        "reason" to reason,
        "status" to status.toString(),
        "importance" to importance.toString(),
        "at" to timestamp.toString(),
    )
}

/**
 * Ship every exit newer than [since], oldest first, and return the timestamp of
 * the last one the server ACTUALLY TOOK — 0 if none got through.
 *
 * The invariant, and the whole reason this is a separate function: the
 * watermark may never move past an entry that was not delivered. The first cut
 * advanced it for entries it had merely attempted, so a null API client at
 * startup silently consumed the exact records the feature existed to capture.
 * A dropped death report is unrecoverable; a duplicated one is noise.
 */
internal suspend fun shipExits(
    exits: List<ExitRecord>,
    since: Long,
    send: suspend (ExitRecord) -> Boolean,
): Long {
    var lastSent = 0L
    for (exit in exits.filter { it.timestamp > since }.sortedBy { it.timestamp }) {
        if (!send(exit)) break
        lastSent = exit.timestamp
    }
    return lastSent
}

/**
 * Ships client-side diagnostics to the server (#2961).
 *
 * The phone is not reachable over adb from the server host, so when playback
 * dies on the device there is no way to find out why — the 2026-08-14 DJ test
 * had to be reconstructed from play-stat rows. Two things travel this way:
 *
 *  - playback errors, via PlaybackManager's onPlayerError;
 *  - **why the process last died**, read from ApplicationExitInfo on the next
 *    start. That is the one signal that explains a silent death after the fact,
 *    and it is why this reports on startup rather than only on failure.
 *
 * Everything here is best-effort and must never throw into a caller: a broken
 * diagnostic channel must not be able to break playback.
 */
@Singleton
class ClientLogReporter @Inject constructor(
    @ApplicationContext private val context: Context,
    private val apiHolder: ApiServiceHolder,
    private val settingsStore: SettingsStore,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    fun report(
        level: String,
        event: String,
        message: String = "",
        detail: Map<String, String> = emptyMap(),
    ) {
        Log.w(TAG, "$event: $message $detail")
        scope.launch { send(level, event, message, detail) }
    }

    /** Returns true only if the server actually took the entry. */
    private suspend fun send(
        level: String,
        event: String,
        message: String,
        detail: Map<String, String>,
    ): Boolean = runCatching {
        val api = apiHolder.api ?: return false
        api.postClientLog(
            ClientLogDto(
                level = level,
                event = event,
                message = message,
                detail = detail,
                at = System.currentTimeMillis() / 1000.0,
            )
        )
        true
    }.getOrDefault(false)

    /**
     * Wait for a usable API client.
     *
     * ApiServiceHolder gets its base URL from the first ViewModel to come up,
     * which is long after Application.onCreate — so anything reporting at
     * startup finds `api` null and must wait rather than drop the report. The
     * first cut of this shipped without the wait and silently lost exactly the
     * process-exit records it existed to capture.
     */
    private suspend fun awaitApi(): Boolean {
        repeat(API_WAIT_ATTEMPTS) {
            if (apiHolder.api != null) return true
            delay(API_WAIT_INTERVAL_MS)
        }
        return apiHolder.api != null
    }

    /**
     * Report any process deaths that happened since we last looked. Answers
     * "why did the app vanish mid-song" — crash, ANR, low memory, or the user.
     *
     * A watermark in settings keeps each death reported exactly once; without
     * it every launch would re-ship the same history.
     */
    fun reportProcessExits() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return
        scope.launch {
            runCatching {
                val since = settingsStore.lastExitReportedAt.first()
                val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
                val exits = am.getHistoricalProcessExitReasons(context.packageName, 0, MAX_EXITS)
                    .map { info ->
                        ExitRecord(
                            timestamp = info.timestamp,
                            reason = reasonName(info.reason),
                            description = info.description ?: reasonName(info.reason),
                            status = info.status,
                            importance = info.importance,
                            unexpected = isUnexpected(info.reason),
                        )
                    }
                if (exits.none { it.timestamp > since }) return@runCatching
                if (!awaitApi()) return@runCatching

                val lastSent = shipExits(exits, since) { exit ->
                    send(
                        level = if (exit.unexpected) "error" else "info",
                        event = "process_exit",
                        message = exit.description,
                        detail = exit.detail(),
                    )
                }
                if (lastSent > 0L) settingsStore.setLastExitReportedAt(lastSent)
            }
        }
    }

    /** Deaths that mean something went wrong, as opposed to a normal teardown. */
    private fun isUnexpected(reason: Int): Boolean = when (reason) {
        ApplicationExitInfo.REASON_CRASH,
        ApplicationExitInfo.REASON_CRASH_NATIVE,
        ApplicationExitInfo.REASON_ANR,
        ApplicationExitInfo.REASON_LOW_MEMORY,
        ApplicationExitInfo.REASON_EXCESSIVE_RESOURCE_USAGE,
        ApplicationExitInfo.REASON_PERMISSION_CHANGE,
        ApplicationExitInfo.REASON_INITIALIZATION_FAILURE -> true
        else -> false
    }

    private fun reasonName(reason: Int): String = when (reason) {
        ApplicationExitInfo.REASON_ANR -> "ANR"
        ApplicationExitInfo.REASON_CRASH -> "CRASH"
        ApplicationExitInfo.REASON_CRASH_NATIVE -> "CRASH_NATIVE"
        ApplicationExitInfo.REASON_DEPENDENCY_DIED -> "DEPENDENCY_DIED"
        ApplicationExitInfo.REASON_EXCESSIVE_RESOURCE_USAGE -> "EXCESSIVE_RESOURCE_USAGE"
        ApplicationExitInfo.REASON_EXIT_SELF -> "EXIT_SELF"
        ApplicationExitInfo.REASON_INITIALIZATION_FAILURE -> "INITIALIZATION_FAILURE"
        ApplicationExitInfo.REASON_LOW_MEMORY -> "LOW_MEMORY"
        ApplicationExitInfo.REASON_OTHER -> "OTHER"
        ApplicationExitInfo.REASON_PERMISSION_CHANGE -> "PERMISSION_CHANGE"
        ApplicationExitInfo.REASON_SIGNALED -> "SIGNALED"
        ApplicationExitInfo.REASON_USER_REQUESTED -> "USER_REQUESTED"
        ApplicationExitInfo.REASON_USER_STOPPED -> "USER_STOPPED"
        else -> "UNKNOWN($reason)"
    }

    companion object {
        private const val TAG = "ClientLog"
        private const val MAX_EXITS = 16
        // Long enough to cover a cold start reaching its first screen, bounded
        // so a never-configured install doesn't hold a coroutine forever.
        private const val API_WAIT_ATTEMPTS = 60
        private const val API_WAIT_INTERVAL_MS = 2000L
    }
}
