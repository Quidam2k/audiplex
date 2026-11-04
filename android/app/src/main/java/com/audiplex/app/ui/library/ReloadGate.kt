package com.audiplex.app.ui.library

/**
 * #5421: the library list used to load once per process, so a book added on
 * the server stayed missing until the app was killed. The screen now reloads
 * quietly on resume; this gate decides when that may run and which load's
 * result may land.
 *
 * - Every load takes a token from [begin]; only the newest token is
 *   [isCurrent], so an older response never overwrites a newer list.
 * - A resume reload is skipped while any load is in flight, and within
 *   [minIntervalMs] of the last successful load (any kind: start-up, URL
 *   change, pull-to-refresh, filter change, resume).
 *
 * Main-thread only, like the ViewModel that owns it.
 */
class ReloadGate(
    private val minIntervalMs: Long = 30_000,
    private val now: () -> Long = { System.nanoTime() / 1_000_000 }
) {
    private var generation = 0
    private var inFlight = 0
    private var lastLoadAt: Long? = null

    fun begin(): Int {
        inFlight++
        return ++generation
    }

    fun isCurrent(token: Int): Boolean = token == generation

    fun end(token: Int, succeeded: Boolean) {
        inFlight--
        if (succeeded && token == generation) lastLoadAt = now()
    }

    fun shouldReloadOnResume(): Boolean {
        if (inFlight > 0) return false
        val last = lastLoadAt ?: return true
        return now() - last >= minIntervalMs
    }
}
