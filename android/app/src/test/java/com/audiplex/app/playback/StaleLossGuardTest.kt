package com.audiplex.app.playback

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** #2345 (Pantheon): the 11/11 19:51:30Z callback order that stranded the music paused. */
class StaleLossGuardTest { // #2345
    /** A tiny main looper: callbacks and the settle marker run in the order they were queued. */
    private class Looper { val q = ArrayDeque<() -> Unit>(); fun post(b: () -> Unit) = q.addLast(b); fun drain() { while (q.isNotEmpty()) q.removeFirst()() } }

    @Test fun theNineteenFiftyOneOrderNoLongerPauses() { // #2345
        val guard = StaleLossGuard()
        val looper = Looper()
        val handled = mutableListOf<String>()
        fun onLoss(name: String) = looper.post { if (!guard.isStale()) handled += name }
        // .532 speech abandons -> our GAIN runs; inside it play() re-requests focus.
        // .550 Talk reclaims MAY_DUCK -> the system queues CAN_DUCK for us (main thread still busy).
        onLoss("can_duck@.550")
        // .562 our re-request comes back GRANTED; the marker goes behind the queued loss.
        val token = guard.onGranted()
        looper.post { guard.onSettled(token) }
        looper.drain()
        assertTrue("the loss queued before our grant must not pause us: $handled", handled.isEmpty())
        // A real loss after the grant has settled still pauses/ducks as before.
        onLoss("real@later")
        looper.drain()
        assertTrue(handled == listOf("real@later"))
    }

    @Test fun noGrantMeansNothingIsStale() { // #2345 behaviour unchanged when we never requested
        assertFalse(StaleLossGuard().isStale())
    }

    @Test fun twoGrantsSettleOnlyWhenTheLatestMarkerRuns() { // #2345
        val g = StaleLossGuard()
        val t1 = g.onGranted(); val t2 = g.onGranted()
        g.onSettled(t1)
        assertTrue(g.isStale())
        g.onSettled(t2)
        assertFalse(g.isStale())
        g.onSettled(t1) // a late old marker never reopens the window
        assertFalse(g.isStale())
    }

    @Test fun managerIsWired() { // #2345 source guard
        val src = File("src/main/java/com/audiplex/app/playback/AudioFocusManager.kt").readText()
        val listener = src.substring(src.indexOf("OnAudioFocusChangeListener {"), src.indexOf("FocusPolicy.decide("))
        assertTrue("stale check runs before the policy", listener.contains("staleLoss.isStale()"))
        assertTrue(src.contains("setOnAudioFocusChangeListener(focusListener, focusHandler)"))
        assertTrue(src.contains("focusHandler.post { staleLoss.onSettled(token) }"))
        assertFalse("holdHandler's queue is cleared wholesale; the marker must not ride it",
            src.contains("holdHandler.post { staleLoss"))
    }
}
