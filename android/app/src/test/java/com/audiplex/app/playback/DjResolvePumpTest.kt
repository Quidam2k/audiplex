package com.audiplex.app.playback

import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.currentTime
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * A big DJ queue resolves in parallel, and the command long-poll keeps going
 * while a command runs (#3249). On 2026-09-28 one lookup at a time, with polling
 * stopped, left the phone dark for ~3 minutes per ~990-track queue.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class DjResolvePumpTest {

    @Test
    fun `resolve preserves input order when fetch delays vary`() = runTest {
        val ids = listOf(10, 20, 30, 40, 50)

        val result = resolveConcurrently(ids, parallelism = ids.size) { id ->
            delay((100 - id) * 10L)
            "track-$id"
        }

        assertEquals(ids.map { "track-$it" }, result)
    }

    @Test
    fun `resolve never exceeds its parallelism`() = runTest {
        val inFlight = AtomicInteger()
        val maximum = AtomicInteger()

        val result = resolveConcurrently((1..50).toList(), parallelism = 8) { id ->
            val active = inFlight.incrementAndGet()
            maximum.updateAndGet { maxOf(it, active) }
            try {
                delay(100)
                id
            } finally {
                inFlight.decrementAndGet()
            }
        }

        assertEquals(50, result.size)
        assertEquals(8, maximum.get())
    }

    @Test
    fun `resolve drops failures and nulls while keeping order`() = runTest {
        val ids = (1..7).toList()

        val result: List<String> = resolveConcurrently(ids, parallelism = 3) { id ->
            when (id) {
                3 -> throw RuntimeException("missing")
                5 -> null
                else -> "track-$id"
            }
        }

        assertEquals(listOf("track-1", "track-2", "track-4", "track-6", "track-7"), result)
    }

    @Test
    fun `resolve completes a large queue within the parallel virtual-time bound`() = runTest {
        assertEquals(8, RESOLVE_PARALLELISM)
        val ids = (1..990).toList()
        val startedAt = currentTime

        val result = resolveConcurrently(ids, RESOLVE_PARALLELISM) { id ->
            delay(170L)
            id
        }

        val elapsed = currentTime - startedAt
        val bound = ((ids.size + RESOLVE_PARALLELISM - 1) / RESOLVE_PARALLELISM) * 170L
        val sequential = ids.size * 170L
        assertEquals(ids, result)
        assertTrue("elapsed $elapsed exceeded bound $bound", elapsed <= bound)
        assertTrue("elapsed $elapsed was too close to sequential $sequential", elapsed * 4 < sequential)
    }

    @Test
    fun `resolve empty input never fetches`() = runTest {
        var fetched = false

        val result = resolveConcurrently<Int>(emptyList()) { id ->
            fetched = true
            id
        }

        assertEquals(emptyList<Int>(), result)
        assertFalse(fetched)
    }

    @Test
    fun `pump keeps polling while a handler is suspended`() = runTest {
        var next = 1
        var pollCalls = 0
        val gate = CompletableDeferred<Unit>()
        val started = mutableListOf<Int>()
        val handled = mutableListOf<Int>()
        val pump = CommandPump(
            poll = {
                pollCalls++
                if (next <= 3) next++ else {
                    delay(1_000)
                    null
                }
            },
            handle = { command ->
                started += command
                if (command == 1) gate.await()
                handled += command
            },
        )

        backgroundScope.launch { pump.run() }
        runCurrent()

        assertTrue(pollCalls >= 4)
        assertEquals(listOf(1), started)
        gate.complete(Unit)
        advanceTimeBy(1)

        assertEquals(listOf(1, 2, 3), handled)
    }

    @Test
    fun `pump runs handlers serially`() = runTest {
        var next = 1
        val events = mutableListOf<String>()
        val pump = CommandPump(
            poll = {
                if (next <= 3) next++ else {
                    delay(1_000)
                    null
                }
            },
            handle = { command ->
                events += "start$command"
                delay(50)
                events += "end$command"
            },
        )

        backgroundScope.launch { pump.run() }
        runCurrent()
        advanceTimeBy(151)

        assertEquals(
            listOf("start1", "end1", "start2", "end2", "start3", "end3"),
            events,
        )
    }

    @Test
    fun `pump survives a handler exception`() = runTest {
        var next = 1
        val handled = mutableListOf<Int>()
        val pump = CommandPump(
            poll = {
                if (next <= 3) next++ else {
                    delay(1_000)
                    null
                }
            },
            handle = { command ->
                if (command == 2) throw RuntimeException("boom")
                handled += command
            },
        )

        backgroundScope.launch { pump.run() }
        runCurrent()

        assertEquals(listOf(1, 3), handled)
    }

    @Test
    fun `redelivery of an in-flight command waits behind it`() = runTest {
        var deliveries = 0
        val results = mutableMapOf<Int, String>()
        val records = mutableListOf<String>()
        val pump = CommandPump(
            poll = {
                if (deliveries++ < 2) 7 else {
                    delay(1_000)
                    null
                }
            },
            handle = { id ->
                if (id in results) {
                    records += "replay:${results.getValue(id)}"
                } else {
                    delay(100)
                    results[id] = "ok"
                    records += "ran"
                }
            },
        )

        backgroundScope.launch { pump.run() }
        runCurrent()
        // Both deliveries are in hand before the first run finishes.
        assertTrue(deliveries >= 2)
        assertEquals(emptyList<String>(), records)
        advanceTimeBy(101)

        assertEquals(listOf("ran", "replay:ok"), records)
    }
}
