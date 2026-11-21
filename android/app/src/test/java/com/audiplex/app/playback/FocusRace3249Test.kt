package com.audiplex.app.playback

import com.audiplex.app.playback.FocusPolicy.FocusAction
import com.audiplex.app.playback.FocusPolicy.FocusState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

private const val GAIN = 1
private const val GAIN_TRANSIENT = 2
private const val LOSS = -1
private const val LOSS_TRANSIENT = -2

/**
 * Regression for #3249: replays the 11/14 21:12:00Z sequence where Pantheon Talk reclaimed focus
 * while Audiplex's GAIN from the persona speech ending was still queued (binder lag). The old
 * behavior re-requested focus on that GAIN, was then hit by the stale LOSS_TRANSIENT, and stayed
 * paused with nobody left to send GAIN. The fix skips the request during a GAIN resume.
 */
class FocusRace3249Test {

    /** Android focus stack semantics with manually delivered (lagging) callbacks. */
    private class FakeFocusStack {
        private val stack = mutableListOf<String>()
        private val queues = mutableMapOf<String, MutableList<Int>>()

        private fun queueOf(client: String) = queues.getOrPut(client) { mutableListOf() }

        fun request(client: String, gainType: Int): Boolean {
            val current = stack.lastOrNull()
            if (current != null && current != client) {
                if (gainType == GAIN) {
                    queueOf(current).add(LOSS)
                    stack.remove(current)
                } else {
                    queueOf(current).add(LOSS_TRANSIENT)
                }
            }
            stack.remove(client)
            stack.add(client)
            return true
        }

        fun abandon(client: String) {
            val wasTop = stack.lastOrNull() == client
            stack.remove(client)
            if (wasTop && stack.isNotEmpty()) {
                queueOf(stack.last()).add(GAIN)
            }
        }

        fun top(): String? = stack.lastOrNull()

        fun deliver(client: String): List<Int> {
            val queue = queueOf(client)
            val pending = queue.toList()
            queue.clear()
            return pending
        }
    }

    private class AudiplexModel(val stack: FakeFocusStack, private val useFix: Boolean) {
        var hasFocus = false
        var pausedByFocus = false
        var wasPlaying = false
        var playing = false
        var inGainResume = false

        fun userPlay() {
            playing = true
            requestFocusOnPlay()
        }

        private fun requestFocusOnPlay() {
            if (useFix && !FocusPolicy.requestOnPlay(inGainResume, hasFocus)) return
            stack.request("audiplex", GAIN)
            hasFocus = true
        }

        private fun onFocus(change: Int) {
            if (change < 0) inGainResume = false
            val action = FocusPolicy.decide(change, FocusState(wasPlaying = wasPlaying))
            when (action) {
                is FocusAction.Restore -> {
                    if (action.resume) {
                        inGainResume = true
                        if (!playing) {
                            playing = true
                            requestFocusOnPlay()
                        }
                        inGainResume = false
                    }
                    wasPlaying = false
                    pausedByFocus = false
                }
                FocusAction.Pause -> {
                    wasPlaying = FocusPolicy.resumeAfterPause(pausedByFocus, wasPlaying, playing, playing)
                    pausedByFocus = true
                    playing = false
                }
                FocusAction.PauseAndAbandon -> {
                    playing = false
                    stack.abandon("audiplex")
                    hasFocus = false
                    pausedByFocus = false
                    wasPlaying = false
                }
                else -> Unit
            }
        }

        fun deliverAll() {
            stack.deliver("audiplex").forEach { onFocus(it) }
        }
    }

    private class PantheonModel(private val stack: FakeFocusStack) {
        fun speech() { stack.request("speech", GAIN_TRANSIENT) }
        fun speechEnd() = stack.abandon("speech")
        fun talkReclaim() { stack.request("talk", GAIN_TRANSIENT) }
        fun talkOff() = stack.abandon("talk")
    }

    private fun runRace(useFix: Boolean, endTalk: Boolean = true): AudiplexModel {
        val stack = FakeFocusStack()
        val audiplex = AudiplexModel(stack, useFix)
        val pantheon = PantheonModel(stack)

        audiplex.userPlay()
        pantheon.speech()
        audiplex.deliverAll()

        pantheon.speechEnd()
        pantheon.talkReclaim()
        audiplex.deliverAll()

        if (endTalk) {
            pantheon.talkOff()
            audiplex.deliverAll()
        }
        return audiplex
    }

    @Test
    fun oldBehaviorStrandsMusicAfterTalkOff() {
        val audiplex = runRace(useFix = false)
        assertFalse(audiplex.playing)
        assertEquals("audiplex", audiplex.stack.top())
    }

    @Test
    fun fixResumesMusicWhenTalkEnds() {
        val audiplex = runRace(useFix = true)
        assertTrue(audiplex.playing)
        assertEquals("audiplex", audiplex.stack.top())
    }

    @Test
    fun fixPausesMusicWhileTalkHolds() {
        val audiplex = runRace(useFix = true, endTalk = false)
        assertFalse(audiplex.playing)
        assertEquals("talk", audiplex.stack.top())
    }

    @Test
    fun userPlayStillTakesFocusFromTalk() {
        val audiplex = runRace(useFix = true, endTalk = false)
        assertFalse(audiplex.playing)

        audiplex.userPlay()

        assertEquals("audiplex", audiplex.stack.top())
        assertTrue(audiplex.playing)
    }

    @Test
    fun requestOnPlayCases() {
        assertFalse(FocusPolicy.requestOnPlay(inGainResume = true, hasFocus = true))
        assertTrue(FocusPolicy.requestOnPlay(inGainResume = true, hasFocus = false))
        assertTrue(FocusPolicy.requestOnPlay(inGainResume = false, hasFocus = true))
        assertTrue(FocusPolicy.requestOnPlay(inGainResume = false, hasFocus = false))
    }
}
