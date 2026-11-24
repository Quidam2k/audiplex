package com.audiplex.app.playback

/**
 * #6837: hands out ONE connected controller however many callers ask while it is
 * still connecting. Before this, every ensureController call during a connect built
 * its own MediaController, and each attached playerListener, so per-track work (the
 * "start" play stat, recently-played, level fetch, pause_source logs) ran 2-4x.
 *
 * Main-thread only, like the rest of PlaybackManager. [startConnect] begins one
 * connect and must call exactly one of its two callbacks, once.
 */
class ControllerGate<T : Any>(
    private val startConnect: (onConnected: (T) -> Unit, onFailed: (Throwable) -> Unit) -> Unit,
) {
    private var current: T? = null
    private var connecting = false
    private var generation = 0
    private val waiting = ArrayList<(T) -> Unit>()

    /** The connected controller, or null while none is up. */
    val connected: T? get() = current

    fun request(onReady: (T) -> Unit) {
        current?.let { onReady(it); return }
        waiting.add(onReady)
        if (connecting) return
        connecting = true
        val gen = ++generation
        startConnect(
            { ctrl ->
                if (gen == generation) {
                    current = ctrl
                    connecting = false
                    val ready = ArrayList(waiting)
                    waiting.clear()
                    ready.forEach { it(ctrl) }
                }
            },
            {
                // A failed connect drops its waiters (their commands cannot run) but
                // leaves the gate open, so the next request tries again.
                if (gen == generation) {
                    connecting = false
                    waiting.clear()
                }
            },
        )
    }

    /**
     * Forget the controller. With [dead] set, only when it is the current one: a
     * late disconnect of an old controller must not undo a newer connect.
     * Any connect still in flight is abandoned.
     */
    fun reset(dead: T? = null) {
        if (dead != null && current !== dead) return
        current = null
        connecting = false
        generation++
        waiting.clear()
    }
}
