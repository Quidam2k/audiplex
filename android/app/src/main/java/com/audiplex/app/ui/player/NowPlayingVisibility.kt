package com.audiplex.app.ui.player

/**
 * Decision logic for when the mini-player should be visible (#3505).
 *
 * The mini-player normally appears when local state (currentBook, currentMusic,
 * or currentStreamTitle) is non-null. However, when the MediaController reports
 * playing (or has a current media item) but local state is null, the UI still
 * needs to show minimal controls so the user can see and pause music that the
 * app is playing via DJ commands but hasn't fully hydrated locally.
 *
 * This function encodes that decision rule as pure logic for testing.
 */
object NowPlayingVisibility {
    /**
     * Should the mini-player show a fallback using controller metadata?
     *
     * @param hasLocalState true if currentBook, currentMusic, or currentStreamTitle is non-null
     * @param controllerHasMetadata true if MediaController reports a current media item or title
     * @param isPlaying true if MediaController.isPlaying is true
     * @return true if fallback should be shown
     */
    fun shouldShowFallback(
        hasLocalState: Boolean,
        controllerHasMetadata: Boolean,
        isPlaying: Boolean
    ): Boolean {
        // If we have local state, never show fallback (normal UI takes over)
        if (hasLocalState) return false

        // If controller has no metadata, nothing to show
        if (!controllerHasMetadata) return false

        // Show fallback when controller has metadata, whether playing or paused
        // (the user may have paused a DJ-commanded song before the app fully hydrated)
        return true
    }
}
