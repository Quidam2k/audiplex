package com.audiplex.app.ui.library

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** #5421: when a silent on-resume library reload may run, and whose result lands. */
class ReloadGateTest {
    private var clock = 0L
    private val gate = ReloadGate(minIntervalMs = 30_000, now = { clock })

    private fun load(succeeded: Boolean = true) {
        val t = gate.begin()
        gate.end(t, succeeded)
    }

    @Test fun `first resume with no load yet reloads`() {
        assertTrue(gate.shouldReloadOnResume())
    }

    @Test fun `resume at 29 s is skipped, at 31 s it reloads`() {
        load()
        clock = 29_000
        assertFalse(gate.shouldReloadOnResume())
        clock = 31_000
        assertTrue(gate.shouldReloadOnResume())
    }

    @Test fun `a manual refresh or URL-change load restarts the 30 s window`() {
        load()
        clock = 31_000
        load() // pull-to-refresh / server URL change go through the same load
        clock = 45_000
        assertFalse(gate.shouldReloadOnResume())
        clock = 61_000
        assertTrue(gate.shouldReloadOnResume())
    }

    @Test fun `a failed load does not start the window, so the next resume retries`() {
        load(succeeded = false)
        clock = 1_000
        assertTrue(gate.shouldReloadOnResume())
    }

    @Test fun `no resume reload while a load is in flight`() {
        val t = gate.begin()
        clock = 60_000
        assertFalse(gate.shouldReloadOnResume())
        gate.end(t, succeeded = true)
        assertFalse(gate.shouldReloadOnResume()) // just loaded
    }

    @Test fun `an older load's result is not current once a newer one starts`() {
        val older = gate.begin()
        val newer = gate.begin()
        assertFalse(gate.isCurrent(older))
        assertTrue(gate.isCurrent(newer))
        gate.end(newer, succeeded = true)
        gate.end(older, succeeded = true)
        clock = 29_000
        assertFalse(gate.shouldReloadOnResume())
    }

    @Test fun `a stale success does not stamp the window`() {
        val older = gate.begin()
        val newer = gate.begin()
        gate.end(older, succeeded = true) // stale, ignored
        gate.end(newer, succeeded = false)
        assertTrue(gate.shouldReloadOnResume())
    }
}
