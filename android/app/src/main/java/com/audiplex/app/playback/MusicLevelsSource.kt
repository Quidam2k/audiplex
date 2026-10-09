package com.audiplex.app.playback

import com.audiplex.app.data.api.MusicLevelsResponse
import kotlinx.coroutines.CancellationException

/**
 * #7528: the server's music levels, re-read on every track change.
 *
 * They used to be fetched once per queue load, so a server target change
 * (-24 -> -20 on 10/9) never reached a queue already playing: the phone kept
 * the old target until the next queue. A failed or impossible fetch (no server
 * configured) keeps the last good levels rather than dropping normalization
 * mid-queue on a network blip.
 */
internal class MusicLevelsSource(private val fetch: suspend () -> MusicLevelsResponse?) {
    var current: MusicLevelsResponse? = null
        private set

    /** Fetch now; true when the levels changed. */
    suspend fun refresh(): Boolean {
        val fresh = try {
            fetch()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            null
        } ?: return false
        val changed = fresh != current
        current = fresh
        return changed
    }
}

/** #7528: the "volume" DJ command moves the dial of the channel that is playing. */
internal fun channelForVolumeCommand(kind: PlayerKind?): PlayerKind =
    if (kind == PlayerKind.Audiobook) PlayerKind.Audiobook else PlayerKind.Music
