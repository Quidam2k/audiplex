package com.audiplex.app.playback

/**
 * #2345 (Pantheon, 11/11 19:51:30Z): a focus LOSS that the system sent BEFORE our latest grant must
 * not pause us. Focus callbacks are posted to the main looper. When another app (Pantheon's Talk)
 * requested focus at .550, its LOSS for us was queued; our own play() re-request at .562 was granted
 * and took the top of the stack, then the queued LOSS ran and paused us. We held focus by then, so
 * no GAIN would ever come and the music stayed stranded until Todd un-paused it by hand.
 *
 * The rule: right after a GRANTED request, post a settle marker to the same looper. Anything queued
 * before the marker was sent before the grant, so a LOSS processed while a grant is unsettled is
 * stale. A loss queued after the marker is real. Pure state; [AudioFocusManager] posts the marker.
 */
class StaleLossGuard { // #2345
    private var granted = 0L
    private var settled = 0L

    /** A request came back GRANTED; returns the token the settle marker carries. */
    @Synchronized fun onGranted(): Long { granted += 1; return granted } // #2345

    /** The marker posted after that grant ran: everything queued before the grant has drained. */
    @Synchronized fun onSettled(token: Long) { if (token > settled) settled = token } // #2345

    /** True when a loss being handled now was queued before our latest grant. */
    @Synchronized fun isStale(): Boolean = settled < granted // #2345
}
