package com.audiplex.app.playback

import com.audiplex.app.data.api.MusicLevelsResponse
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** #7528: the music levels are re-read on every track change. */
class MusicLevelsRefreshTest {
    private fun levels(target: Double) = MusicLevelsResponse(normalizeMusic = true, targetLufs = target)

    /** 10/9: the server went -24 -> -20 and the phone kept -24 for the whole queue. */
    @Test fun aServerTargetChangeReachesTheNextSong() = runTest {
        var serverTarget = -24.0
        val source = MusicLevelsSource { levels(serverTarget) }
        assertTrue(source.refresh())  // queue load
        val before = LoudnessGain.gainFor(-14.0, source.current!!.targetLufs, null)

        serverTarget = -20.0
        assertTrue(source.refresh())  // the next track change
        val after = LoudnessGain.gainFor(-14.0, source.current!!.targetLufs, null)

        assertEquals(-20.0, source.current!!.targetLufs, 0.0)
        assertTrue("a -20 target is louder than -24", after > before)
    }

    @Test fun unchangedLevelsDoNotReapply() = runTest {
        val source = MusicLevelsSource { levels(-20.0) }
        source.refresh()
        assertFalse(source.refresh())
    }

    @Test fun aFailedFetchKeepsTheLastGoodLevels() = runTest {
        var fail = false
        val source = MusicLevelsSource { if (fail) error("tailscale blip") else levels(-20.0) }
        source.refresh()
        fail = true
        assertFalse(source.refresh())
        assertEquals(-20.0, source.current!!.targetLufs, 0.0)
    }

    @Test fun noServerKeepsTheLastGoodLevels() = runTest {
        var configured = true
        val source = MusicLevelsSource { if (configured) levels(-20.0) else null }
        source.refresh()
        configured = false
        assertFalse(source.refresh())
        assertEquals(-20.0, source.current!!.targetLufs, 0.0)
    }

    @Test fun theVolumeCommandMovesTheMusicDial() {
        assertEquals(PlayerKind.Music, channelForVolumeCommand(PlayerKind.Music))
        assertEquals(PlayerKind.Music, channelForVolumeCommand(PlayerKind.Stream))
        assertEquals(PlayerKind.Music, channelForVolumeCommand(null))
        assertEquals(PlayerKind.Audiobook, channelForVolumeCommand(PlayerKind.Audiobook))
    }
}
