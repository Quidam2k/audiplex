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
                    .filter { it.timestamp > since }
                    .sortedBy { it.timestamp }
                if (exits.isEmpty()) return@runCatching
                if (!awaitApi()) return@runCatching

                // Advance the watermark only past entries the server actually
                // took. Anything unsent stays unmarked and is retried on the
                // next launch — a dropped death report is unrecoverable, a
                // duplicated one is merely noise.
                var lastSent = 0L
                for (info in exits) {
                    val ok = send(
                        level = if (isUnexpected(info.reason)) "error" else "info",
                        event = "process_exit",
                        message = info.description ?: reasonName(info.reason),
                        detail = mapOf(
                            "reason" to reasonName(info.reason),
                            "status" to info.status.toString(),
                            "importance" to info.importance.toString(),
                            "at" to info.timestamp.toString(),
                        ),
                    )
                    if (!ok) break
                    lastSent = info.timestamp
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
