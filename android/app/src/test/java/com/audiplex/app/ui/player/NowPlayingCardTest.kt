package com.audiplex.app.ui.player

import com.audiplex.app.data.api.TrackSchema
import com.audiplex.app.playback.MusicQueueItem
import com.audiplex.app.playback.MusicQueueState
import com.audiplex.app.playback.ORIGIN_DJ
import com.audiplex.app.playback.ORIGIN_MANUAL
import com.audiplex.app.playback.PlayerKind
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** #7528: the Now playing card on both home screens. */
class NowPlayingCardTest {
    private fun queue(n: Int, index: Int, origin: String? = ORIGIN_DJ) = MusicQueueState(
        items = (1..n).map {
            MusicQueueItem(
                track = TrackSchema(id = it, title = "Song $it", albumId = 1, artistId = 1, artistName = "Artist",
                    discNumber = 1, trackNumber = it, durationSeconds = 200.0),
                albumId = 1, albumTitle = "Album", albumHasCover = false,
            )
        },
        albumId = null, playlistId = null, title = "Now playing", currentIndex = index, origin = origin,
    )

    @Test fun showsForADjQueue() {
        val m = nowPlayingCardModel(PlayerKind.Music, queue(312, 17))!!
        assertEquals("DJ queue · 18/312", m.queueLabel)
        assertEquals("Song 18", m.title)
        assertEquals(listOf("Song 19", "Song 20", "Song 21"), m.upNext)
    }

    @Test fun aManualQueueUsesItsTitle() =
        assertEquals("Now playing · 1/5", nowPlayingCardModel(PlayerKind.Music, queue(5, 0, ORIGIN_MANUAL))!!.queueLabel)

    @Test fun upNextShortensAtTheEnd() =
        assertEquals(listOf("Song 5"), nowPlayingCardModel(PlayerKind.Music, queue(5, 3))!!.upNext)

    @Test fun hiddenWithoutMusic() {
        assertNull(nowPlayingCardModel(PlayerKind.Audiobook, queue(5, 0)))
        assertNull(nowPlayingCardModel(PlayerKind.Music, null))
        assertNull(nowPlayingCardModel(null, null))
    }
}
