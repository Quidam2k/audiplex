package com.audiplex.app.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** #5282 slice 2. */
class MixLevelsTest {

    /** A queue through the real steering. Normalization off: the ordinary song path does nothing. */
    private class Session(val items: List<Boolean>) {
        var index = 0
        var dial = 0.5f
        val voice = 0.2f
        var volume = 0.5f
        var songChanges = 0
        val steering = ClipVoiceSteering(
            currentIsClip = { items[index] },
            writeLevel = { write() },
            songChange = { songChanges++ },
        )

        fun write() {
            volume = MixLevels.split(MixLevels.musicAmplitude(items[index], dial, 1.0, voice, 1f)).volume
        }

        fun goTo(i: Int) {
            index = i
            steering.onItemTransition()
        }
    }

    private val song = false
    private val clip = true

    @Test
    fun autoAdvanceIntoAClipPlaysAtTheVoiceLevelNotTheDial() {
        val s = Session(listOf(song, clip, song))
        s.goTo(1)
        assertEquals(0.2f, s.volume, 1e-4f)
    }

    @Test
    fun skipOverASongOntoAClipPlaysAtTheVoiceLevel() {
        val s = Session(listOf(song, song, clip))
        s.goTo(2)
        assertEquals(0.2f, s.volume, 1e-4f)
    }

    @Test
    fun previousBackIntoAClipPlaysAtTheVoiceLevel() {
        val s = Session(listOf(song, clip, song))
        s.goTo(1)
        s.goTo(2)
        assertEquals(0.5f, s.volume, 1e-4f)
        s.goTo(1)
        assertEquals(0.2f, s.volume, 1e-4f)
    }

    @Test
    fun aSongAfterAClipReturnsToTheDialEvenWithTheVoiceFarUnderIt() {
        val s = Session(listOf(clip, song))
        s.goTo(0)
        s.goTo(1)
        assertEquals(0.5f, s.volume, 1e-4f)
    }

    @Test
    fun clipToClipStaysAtTheVoiceLevelAfterSomethingWroteTheDial() {
        val s = Session(listOf(clip, clip))
        s.goTo(0)
        s.volume = s.dial  // a play entry writes the music dial before the new item arrives
        s.goTo(1)
        assertEquals(0.2f, s.volume, 1e-4f)
    }

    @Test
    fun rewritingTheLevelMidClipLandsOnTheVoiceLevel() {
        val s = Session(listOf(song, clip))
        s.goTo(1)
        s.volume = 0f  // a sleep fade left it silent
        s.write()
        assertEquals(0.2f, s.volume, 1e-4f)
    }

    @Test
    fun movingTheMusicDialDuringAClipDoesNotMoveTheVoice() {
        val s = Session(listOf(song, clip))
        s.goTo(1)
        s.dial = 1f
        s.write()
        assertEquals(0.2f, s.volume, 1e-4f)
    }

    @Test
    fun songToSongStaysOnTheOrdinaryPath() {
        val s = Session(listOf(song, song))
        s.volume = 0.3f
        s.goTo(1)
        assertEquals(1, s.songChanges)
        assertEquals(0.3f, s.volume, 1e-4f)
    }

    @Test
    fun theVoiceTakesTheBedtimeCutLikeEverythingElse() {
        assertEquals(0.1, MixLevels.musicAmplitude(true, 1f, 1.0, 0.2f, 0.5f), 1e-4)
    }

    @Test
    fun theSongIsSilentAtEachClipBoundary() {
        assertEquals(0f, ClipEdgeFade.level(true, false, false, 0L, 180_000L), 1e-4f)
        assertTrue(ClipEdgeFade.level(true, false, false, ClipEdgeFade.TICK_MS, 180_000L) < 0.06f)
        assertEquals(0f, ClipEdgeFade.level(false, false, true, 180_000L, 180_000L), 1e-4f)
    }

    @Test
    fun aClipStartsFromSilenceSoThePreviousSongsVolumeIsNotHeard() {
        assertEquals(0f, ClipEdgeFade.level(false, true, false, 0L, 5_000L), 1e-4f)
        assertTrue(ClipEdgeFade.level(false, true, false, ClipEdgeFade.TICK_MS, 5_000L) < 0.6f)
        assertEquals(1f, ClipEdgeFade.level(false, true, false, ClipEdgeFade.CLIP_IN_MS, 5_000L), 1e-4f)
    }

    @Test
    fun upToFullIsVolumeAndPastFullGoesThroughTheLimiter() {
        assertEquals(MixLevels.Split(0.5f, 0.0), MixLevels.split(0.5))
        assertEquals(MixLevels.Split(1f, 0.0), MixLevels.split(1.0))
        assertEquals(MixLevels.Split(0f, 0.0), MixLevels.split(0.0))
        assertEquals(MixLevels.Split(0f, 0.0), MixLevels.split(Double.NaN))
        val past = MixLevels.split(2.0)
        assertEquals(1f, past.volume, 1e-4f)
        assertEquals(6.02, past.boostDb, 0.01)
        assertEquals(12.0, MixLevels.split(7.94).boostDb, 1e-6)  // +18 dB asked: slider +12 and a server raise +6
    }

    @Test
    fun aVoicePastFullDoesNotPushTheSongsPastFull() {
        assertEquals(1.0, MixLevels.musicAmplitude(false, 1f, 1.0, 3f, 1f), 1e-4)
        assertEquals(3.0, MixLevels.musicAmplitude(true, 1f, 1.0, 3f, 1f), 1e-4)
    }

    @Test
    fun theSliderRunsFromSilentThroughFullToTwelvePast() {
        assertEquals(0f, MixLevels.sliderToLevel(0f), 1e-4f)
        assertEquals(1f, MixLevels.sliderToLevel(MixLevels.FULL_POSITION), 1e-4f)
        assertEquals(MixLevels.MAX_LEVEL, MixLevels.sliderToLevel(1f), 1e-3f)
        assertEquals(MixLevels.FULL_POSITION, MixLevels.levelToSlider(1f), 1e-4f)
        assertEquals(0.5f, MixLevels.sliderToLevel(MixLevels.levelToSlider(0.5f)), 1e-3f)
        assertEquals(2f, MixLevels.sliderToLevel(MixLevels.levelToSlider(2f)), 1e-3f)
        assertEquals("Off", MixLevels.levelText(0f))
        assertEquals("Full", MixLevels.levelText(1f))
        assertEquals("-6 dB", MixLevels.levelText(0.5f))
        assertEquals("+6 dB, can distort", MixLevels.levelText(2f))
    }

    @Test
    fun fullIsStickySoAHairPastItDoesNotEngageTheLimiter() {
        assertEquals(1f, MixLevels.sliderToLevel(MixLevels.levelToSlider(1.05f)), 1e-4f)
        assertEquals(0.9f, MixLevels.sliderToLevel(MixLevels.levelToSlider(0.9f)), 1e-3f)  // the bed's default isn't moved
        assertEquals("-1 dB", MixLevels.levelText(0.9f))
    }

    @Test
    fun chimeLevelTrimsChimesAndLeavesBedsAlone() {
        assertEquals(0.06f, MixLevels.bedCommandLevel("http://solace:8100/api/dj/chimes/q15", 0.12f, 0.5f), 1e-4f)
        assertEquals(0.06f, MixLevels.bedCommandLevel("http://100.1.2.3:8100/api/dj/chimes/hour_05", 0.12f, 0.5f), 1e-4f)
        assertEquals(0.12f, MixLevels.bedCommandLevel("http://solace:8100/api/dj/chimes/q15", 0.12f, 1f), 1e-4f)
        assertEquals(0.48f, MixLevels.bedCommandLevel("http://solace:8100/api/dj/chimes/q15", 0.12f, 4f), 1e-4f)  // +12 dB
        assertEquals(0.9f, MixLevels.bedCommandLevel("http://solace:8100/api/dj/beds/1213/stream", 0.9f, 0.5f), 1e-4f)
    }

    @Test
    fun djVoiceStartsAtTheDialItUsedToFollowAndOnlyOnce() {
        assertEquals(0.5f, MixLevels.seedDjVoice(null, 0.5f)!!, 1e-4f)
        assertEquals(1f, MixLevels.seedDjVoice(null, null)!!, 1e-4f)
        assertNull(MixLevels.seedDjVoice(0.7f, 0.5f))
    }

    @Test
    fun onlyAClearAnswerFromTheServerCounts() {
        assertEquals(true, MixLevels.raisesOff("attenuate_only"))
        assertEquals(false, MixLevels.raisesOff("boost_needs_limiter"))
        assertNull(MixLevels.raisesOff(null))
        assertNull(MixLevels.raisesOff("something_new"))
    }

    @Test
    fun bedtimeShiftCannotRunTwice() {
        val first = MixLevels.bedtimeShift(0.36f, null, raisesOff = true)!!
        assertEquals(0.718f, first.level, 1e-3f)
        assertEquals(6f, first.appliedDb!!, 1e-3f)
        assertEquals(0f, first.cappedDb, 1e-3f)
        assertNull(MixLevels.bedtimeShift(first.level, first.appliedDb, raisesOff = true))
    }

    @Test
    fun aServerRollbackPutsTheBalanceBackAndAReflipMatches() {
        val flip = MixLevels.bedtimeShift(0.36f, null, raisesOff = true)!!
        val back = MixLevels.bedtimeShift(flip.level, flip.appliedDb, raisesOff = false)!!
        assertEquals(0.36f, back.level, 1e-3f)
        assertNull(back.appliedDb)
        assertEquals(0f, back.cappedDb, 1e-3f)
        assertNull(MixLevels.bedtimeShift(back.level, back.appliedDb, raisesOff = false))
        assertEquals(flip.level, MixLevels.bedtimeShift(back.level, null, raisesOff = true)!!.level, 1e-3f)
    }

    @Test
    fun nearFullTheCapIsReportedNotHiddenAndRollsBackExactly() {
        val flip = MixLevels.bedtimeShift(0.9f, null, raisesOff = true)!!
        assertEquals(1f, flip.level, 1e-3f)
        assertEquals(0.915f, flip.appliedDb!!, 1e-3f)
        assertEquals(5.085f, flip.cappedDb, 1e-3f)
        assertEquals(0.9f, MixLevels.bedtimeShift(flip.level, flip.appliedDb, raisesOff = false)!!.level, 1e-3f)
    }

    @Test
    fun atFullNothingMovesAndTheWholeShiftIsReported() {
        val flip = MixLevels.bedtimeShift(1f, null, raisesOff = true)!!
        assertEquals(1f, flip.level, 1e-4f)
        assertEquals(0f, flip.appliedDb!!, 1e-4f)
        assertEquals(6f, flip.cappedDb, 1e-4f)
        assertNull(MixLevels.bedtimeShift(flip.level, flip.appliedDb, raisesOff = true))
        assertEquals(1f, MixLevels.bedtimeShift(flip.level, flip.appliedDb, raisesOff = false)!!.level, 1e-4f)
    }

    @Test
    fun silentStaysSilentAndIsNotACap() {
        val flip = MixLevels.bedtimeShift(0f, null, raisesOff = true)!!
        assertEquals(0f, flip.level, 1e-4f)
        assertEquals(0f, flip.cappedDb, 1e-4f)
        assertNull(MixLevels.bedtimeShift(flip.level, flip.appliedDb, raisesOff = true))
    }
}
