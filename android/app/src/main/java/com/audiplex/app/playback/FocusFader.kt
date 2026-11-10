package com.audiplex.app.playback

/**
 * #5834: the volume side of audio focus (duck, restore, focus-pause fade), moved out of
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
    /** #5834: the dial level while a focus-pause fade runs (what a GAIN mid-fade restores
     *  to); null when no fade is running. */
    private var pauseFadeLevel: Float? = null
    /** #5834: the volume to leave on the paused player, so a later play is never silent. */
    private var afterPauseLevel: Float? = null

    /** #5834: the level a GAIN should restore to: the fade's dial level, a rise already
     *  under way (a second GAIN mid-rise must not aim at the half-risen volume), else the
     *  pre-duck one. */
    val restoreTarget: Float? get() = pauseFadeLevel ?: restoringTo ?: preDuckVolume

    /** #5834: a focus-pause fade is running (its own pause has not fired yet). */
    val isFading: Boolean get() = pauseFadeLevel != null

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
        pauseFadeLevel?.let { port.volume = it }  // #5834 an abandon mid-fade lands on the dial level
        pauseFadeLevel = null
        afterPauseLevel = null
    }

    /**
     * #5834: a transient focus loss fades out over [DuckRamp.PAUSE_FADE_MS], then pauses
     * ([beforePause] runs just before the pause). A restore or duck that lands mid-fade
     * takes over the ramp, so no pause happens. Not playing: no fade, the old instant path.
     */
    fun fadeOutAndPause(beforePause: () -> Unit) {
        if (!port.isPlaying) {
            cancelRamp()
            beforePause()
            port.pause()
            return
        }
        if (pauseFadeLevel != null) return  // #5834 a repeat loss keeps the running fade and its deadline
        val level = restoringTo ?: preDuckVolume ?: port.volume
        // What the pre-#5834 instant pause left on the player: a restore cut short
        // landed on its target, a duck stayed at the duck level.
        afterPauseLevel = restoringTo ?: port.volume
        restoringTo = null  // a rise cut short is NOT landed first: no jump up before the fade
        pauseFadeLevel = level
        rampTo(0f, DuckRamp.PAUSE_FADE_MS) {
            val after = if (preDuckVolume != null) afterPauseLevel else pauseFadeLevel
            pauseFadeLevel = null
            afterPauseLevel = null
            beforePause()
            port.pause()
            after?.let { port.volume = it }  // paused, so inaudible; a user play is at level
        }
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
    fun retarget(volume: Float): Boolean {
        if (pauseFadeLevel != null) {  // #5834 mid-fade: the fade keeps the new level for after
            pauseFadeLevel = volume
            if (preDuckVolume != null) preDuckVolume = volume
            return true
        }
        return when (FocusPolicy.retargetFor(preDuckVolume, restoringTo)) {
            FocusPolicy.Retarget.PRE_DUCK -> { preDuckVolume = volume; true }
            FocusPolicy.Retarget.RESTORE_RAMP -> {
                restoringTo = volume
                rampTo(volume, DuckRamp.RESTORE_MS, DuckRamp::restoreLevel) { restoringTo = null }
                true
            }
            FocusPolicy.Retarget.NONE -> false
        }
    }

    fun duck(volume: Float) {
        // Only capture the dial level on the first duck; repeated CAN_DUCK
        // events while already ducked must not overwrite it with 0.05.
        if (preDuckVolume == null) preDuckVolume = pauseFadeLevel ?: restoringTo ?: port.volume  // #3597 #5834
        restoringTo = null
        pauseFadeLevel = null  // #5834 a duck mid-fade takes over: no pause
        afterPauseLevel = null
        rampTo(volume, DuckRamp.DUCK_MS)  // #3597 was an instant snap
    }

    fun restore(volume: Float, resume: Boolean, onDone: () -> Unit = {}) {
        pauseFadeLevel = null  // #5834 a GAIN mid-fade takes over the ramp: no pause
        afterPauseLevel = null
        val wake = resume && !port.isPlaying
        if (wake) port.volume = DuckRamp.MIN_GAIN  // #5834 a focus pause resumes from silence
        restoringTo = volume  // #3597
        rampTo(volume, DuckRamp.RESTORE_MS, DuckRamp::restoreLevel) { restoringTo = null; onDone() }  // #3597 Pantheon #2187
        if (wake) port.play()
        preDuckVolume = null
    }
}
