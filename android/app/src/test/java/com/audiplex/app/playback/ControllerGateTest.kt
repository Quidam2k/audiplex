package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

/** #6837: rapid ensureController calls must yield one controller, once. */
class ControllerGateTest {
    private class Fake(val name: String)

    /** A gate whose connect finishes only when the test says so. */
    private class Harness {
        var connects = 0
        var finish: ((Fake) -> Unit)? = null
        var fail: ((Throwable) -> Unit)? = null
        val gate = ControllerGate<Fake> { ok, bad -> connects++; finish = ok; fail = bad }
    }

    @Test
    fun `rapid requests while connecting make one connect and run in order`() {
        val h = Harness()
        val seen = mutableListOf<String>()
        repeat(5) { i -> h.gate.request { seen.add("$i:${it.name}") } }
        assertEquals(1, h.connects)
        assertEquals(emptyList<String>(), seen)
        h.finish!!(Fake("c"))
        assertEquals(listOf("0:c", "1:c", "2:c", "3:c", "4:c"), seen)
    }

    @Test
    fun `listener setup runs once however many callers wait`() {
        var listenersAttached = 0
        val gate = ControllerGate<Fake> { ok, _ -> listenersAttached++; ok(Fake("c")) }
        repeat(4) { gate.request { } }
        assertEquals(1, listenersAttached)
    }

    @Test
    fun `request after connected runs at once with no new connect`() {
        val h = Harness()
        h.gate.request { }
        val c = Fake("c")
        h.finish!!(c)
        var got: Fake? = null
        h.gate.request { got = it }
        assertSame(c, got)
        assertEquals(1, h.connects)
    }

    @Test
    fun `reset then request connects again`() {
        val h = Harness()
        h.gate.request { }
        h.finish!!(Fake("a"))
        h.gate.reset()
        assertNull(h.gate.connected)
        var got: Fake? = null
        h.gate.request { got = it }
        assertEquals(2, h.connects)
        h.finish!!(Fake("b"))
        assertEquals("b", got?.name)
    }

    @Test
    fun `a failed connect drops its waiters and the next request retries`() {
        val h = Harness()
        var ran = 0
        h.gate.request { ran++ }
        h.gate.request { ran++ }
        h.fail!!(RuntimeException("no session"))
        assertEquals(0, ran)
        h.gate.request { ran++ }
        assertEquals(2, h.connects)
        h.finish!!(Fake("b"))
        assertEquals(1, ran)
    }

    @Test
    fun `a late disconnect of an old controller does not undo a newer one`() {
        val h = Harness()
        val old = Fake("old")
        h.gate.request { }
        h.finish!!(old)
        h.gate.reset(old)
        h.gate.request { }
        val fresh = Fake("fresh")
        h.finish!!(fresh)
        h.gate.reset(old)
        assertSame(fresh, h.gate.connected)
    }

    @Test
    fun `a connect abandoned by reset cannot land afterwards`() {
        val h = Harness()
        h.gate.request { }
        val stale = h.finish!!
        h.gate.reset()
        stale(Fake("late"))
        assertNull(h.gate.connected)
    }
}
