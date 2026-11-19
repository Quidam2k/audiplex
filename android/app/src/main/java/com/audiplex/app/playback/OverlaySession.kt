package com.audiplex.app.playback

/**
 * #5695: one overlay clip's lifecycle. Armed -> Playing -> Done. Whatever ends it (the clip
 * ends or errors, the cap, a persona taking focus, a newer overlay, release) goes through
 * one idempotent finish(), which undoes the music duck exactly once, first, before anything
 * that could throw. An overlay that is dropped is never retried, here or anywhere else.
 */
class OverlaySession(
    private val port: Port,
    private val scheduler: Scheduler,
    val clipId: Long,
    private val forTrackId: Int,
    private val startAtMs: Long,
    private val duckTo: Float,
    private val capMs: Long,
    private val armedAtMs: Long,
) {
    interface Port {
        fun view(): OverlayRules.View
        fun duckMusic(fraction: Float): Boolean
        fun restoreMusic()
        fun playClip()
        fun stopClip()
        fun log(event: String, detail: Map<String, String>)
        fun onFinished()
    }

    interface Scheduler {
        fun postDelayed(delayMs: Long, block: () -> Unit)
        fun cancelAll()
    }

    enum class State { Armed, Playing, Done }

    var state: State = State.Armed
        private set

    private var dueSinceMs: Long? = null
    private var started = false
    private var ducked = false

    fun tick(nowMs: Long) {
        if (state != State.Armed) return
        val view = try {
            port.view()
        } catch (e: Exception) {
            finish("error", "view_failed: ${e.message}")
            return
        }
        if (dueSinceMs == null && view.positionMs >= startAtMs && view.playing) {
            dueSinceMs = nowMs
        }
        when (val s = OverlayRules.step(view, forTrackId, startAtMs, armedAtMs, dueSinceMs, nowMs)) {
            OverlayRules.Step.Wait -> Unit
            is OverlayRules.Step.Drop -> finish("dropped", s.reason)
            OverlayRules.Step.Start -> start()
        }
    }

    private fun start() {
        val accepted = try {
            port.duckMusic(duckTo)
        } catch (e: Exception) {
            // #5695 a throwing duck is treated as not ducked
            finish("error", "duck_failed: ${e.message}")
            return
        }
        if (!accepted) {
            finish("dropped", "duck_refused")
            return
        }
        ducked = true
        started = true
        state = State.Playing
        try {
            port.playClip()
        } catch (e: Exception) {
            finish("error", "play_failed: ${e.message}")
            return
        }
        try {
            port.log("overlay_start", mapOf("clip_id" to clipId.toString()))
        } catch (_: Exception) {
        }
        try {
            scheduler.postDelayed(capMs) { finish("capped", "cap_${capMs}ms") }
        } catch (e: Exception) {
            finish("error", "schedule_failed: ${e.message}")
        }
    }

    fun onClipEnded() = finish("ended", "clip_ended")

    fun onClipError(msg: String) = finish("error", msg)

    fun onPreempted(reason: String) = finish("preempted", reason)

    fun cancel(reason: String) = finish("cancelled", reason)

    private fun finish(outcome: String, reason: String) {
        if (state == State.Done) return
        val wasStarted = started
        val wasDucked = ducked
        state = State.Done
        ducked = false
        started = false

        // #5695 restore first; each call isolated so one failure never skips the rest
        if (wasDucked) {
            try {
                port.restoreMusic()
            } catch (_: Exception) {
            }
        }
        try {
            scheduler.cancelAll()
        } catch (_: Exception) {
        }
        if (wasStarted) {
            try {
                port.stopClip()
            } catch (_: Exception) {
            }
        }
        try {
            port.log(
                "overlay_end",
                mapOf("outcome" to outcome, "reason" to reason, "clip_id" to clipId.toString()),
            )
        } catch (_: Exception) {
        }
        try {
            port.onFinished()
        } catch (_: Exception) {
        }
    }

    companion object {
        const val POLL_MS = 100L
    }
}
