package com.audiplex.app.playback

import android.media.AudioManager
import org.junit.Assert.assertEquals
import org.junit.Test

/** #7528: a track change mid-duck moves where the restore lands. */
class DuckRetargetTest {
    @Test fun notDuckedSetsTheVolumeDirectly() =
        assertEquals(FocusPolicy.Retarget.NONE, FocusPolicy.retargetFor(preDuckVolume = null, restoringTo = null))

    @Test fun duckedRetargetsThePreDuckLevel() =
        assertEquals(FocusPolicy.Retarget.PRE_DUCK, FocusPolicy.retargetFor(preDuckVolume = 0.8f, restoringTo = null))

    @Test fun midRestoreReaimsTheRamp() =
        assertEquals(FocusPolicy.Retarget.RESTORE_RAMP, FocusPolicy.retargetFor(preDuckVolume = null, restoringTo = 0.8f))

    /** Slider 0.8, the new song's gain 0.5: the restore lands on 0.4, not the old song's 0.8. */
    @Test fun theRestoreLandsOnSliderTimesTheNewGain() {
        val newLevel = 0.8f * 0.5f
        val ducked = FocusPolicy.FocusState(currentVolume = FocusPolicy.DUCK_VOLUME, preDuckVolume = 0.8f)
        // What retargetRestore does in the PRE_DUCK case:
        val retargeted = ducked.copy(preDuckVolume = newLevel)
        assertEquals(
            FocusPolicy.FocusAction.Restore(volume = 0.4f, resume = false),
            FocusPolicy.decide(AudioManager.AUDIOFOCUS_GAIN, retargeted),
        )
    }
}
