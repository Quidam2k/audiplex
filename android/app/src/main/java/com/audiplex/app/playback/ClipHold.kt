package com.audiplex.app.playback

/**
 * #5837 F: a DJ clip that starts while Audiplex is ducked under another app's audio (a
 * persona line) waits paused for that app's GAIN, at most [DjClipRules.HOLD_CAP_MS].
 * Pure (ports in, like [FocusFader]) so the JVM tests pin it; [AudioFocusManager] owns one.
 */
class ClipHold(private val port: Port, private val scheduler: FocusFader.Scheduler) {
    interface Port {
        val ducked: Boolean
        val playWhenReady: Boolean
        /** Mark the coming pause as ours, so the user-pause path keeps focus and the GAIN resumes. */
        fun markOurPause()
        /** Forget it: at the cap the clip plays now, and a later GAIN must not "resume" anything. */
        fun clearOurPause()
        fun pause()
        fun play()
        fun log(message: String)
    }

    var held = false
        private set

    /** A media item is starting. Returns true when it held a DJ clip. */
    fun onItemStart(mediaId: String?): Boolean {
        if (held || !DjClipRules.shouldHold(mediaId, port.ducked, port.playWhenReady)) return false
        held = true
        port.markOurPause()
        port.pause()
        scheduler.postDelayed(DjClipRules.HOLD_CAP_MS) { release(atCap = true) }
        port.log("#5837 DJ clip held: another app is speaking")
        return true
    }

    /** Any play (the GAIN's resume, or Todd's tap) ends the hold. */
    fun onPlayRequested() {
        if (held) release(atCap = false)
    }

    /** Focus abandoned or the service is going away. */
    fun reset() {
        held = false
        scheduler.cancelAll()
    }

    private fun release(atCap: Boolean) {
        if (!held) return
        held = false
        scheduler.cancelAll()
        if (atCap) {
            // Still on after 20 s: play the clip at the duck level (the duck stays the
            // rule under another app); the GAIN restores it.
            port.clearOurPause()
            port.log("#5837 DJ clip hold hit the ${DjClipRules.HOLD_CAP_MS / 1000} s cap: playing it ducked")
            port.play()
        } else {
            port.log("#5837 DJ clip hold released")
        }
    }
}
