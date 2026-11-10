package com.audiplex.app.playback

/**
 * #5834: the volume side of audio focus (duck, restore ramps), moved out of
 * [AudioFocusManager] unchanged so JVM tests can drive it with a fake clock.
 * [AudioFocusManager] keeps the focus request and the pause/resume flags.
 */
class FocusFader(private val port: Port, private val scheduler: Scheduler) {
    interface Port {
        var volume: Float
        val isPlaying: Boolean
        fun pause()
        fun play()
    }

    interface Scheduler {
        fun postDelayed(delayMs: Long, block: () -> Unit)
        fun cancelAll()
    }

    /** Volume before we ducked; null when not ducked. */
    var preDuckVolume: Float? = null
        private set

    // #3597 duck/restore ramps (see DuckRamp).
    private var rampToken = 0
    /** The dial level a restore ramp is heading to; a duck that interrupts it must
     *  remember THIS, not the half-risen volume. Null when no restore is running. */
    private var restoringTo: Float? = null

    private fun rampTo(
        target: Float, durationMs: Long,
        curve: (Int, Int, Float, Float) -> Float = DuckRamp::level,  // Pantheon #2187
        onDone: () -> Unit = {},
    ) {
        val token = ++rampToken  // cancels any ramp already running
        scheduler.cancelAll()
        val from = port.volume
        val steps = DuckRamp.steps(durationMs)
        fun step(i: Int) {
            if (token != rampToken) return
            port.volume = curve(i, steps, from, target)  // Pantheon #2187
            if (i >= steps) onDone() else scheduler.postDelayed(DuckRamp.STEP_MS) { step(i + 1) }
        }
        step(1)
    }

    /** Stop any ramp. A restore cut short lands on its target at once, so a pause or
     *  abandon mid-rise never leaves the dial stuck partway. */
    fun cancelRamp() {
        rampToken++
        scheduler.cancelAll()
        restoringTo?.let { port.volume = it }
        restoringTo = null
    }

    /** Abandon: stop any ramp and forget the duck. */
    fun reset() {
        cancelRamp()  // #3597
        preDuckVolume = null
    }

    /**
     * #7528: a new dial/track level arrived. Ducked: remember it as the restore
     * level. Mid-restore: re-aim the rising ramp at it. Returns false when
     * neither, so the caller sets the volume as usual.
     */
    fun retarget(volume: Float): Boolean =
        when (FocusPolicy.retargetFor(preDuckVolume, restoringTo)) {
            FocusPolicy.Retarget.PRE_DUCK -> { preDuckVolume = volume; true }
            FocusPolicy.Retarget.RESTORE_RAMP -> {
                restoringTo = volume
                rampTo(volume, DuckRamp.RESTORE_MS, DuckRamp::restoreLevel) { restoringTo = null }
                true
            }
            FocusPolicy.Retarget.NONE -> false
        }

    fun duck(volume: Float) {
        // Only capture the dial level on the first duck; repeated CAN_DUCK
        // events while already ducked must not overwrite it with 0.05.
        if (preDuckVolume == null) preDuckVolume = restoringTo ?: port.volume  // #3597
        restoringTo = null
        rampTo(volume, DuckRamp.DUCK_MS)  // #3597 was an instant snap
    }

    fun restore(volume: Float, resume: Boolean) {
        restoringTo = volume  // #3597
        rampTo(volume, DuckRamp.RESTORE_MS, DuckRamp::restoreLevel) { restoringTo = null }  // #3597 Pantheon #2187
        if (resume && !port.isPlaying) port.play()
        preDuckVolume = null
    }
}
