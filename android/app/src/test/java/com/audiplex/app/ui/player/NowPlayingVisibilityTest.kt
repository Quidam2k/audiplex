package com.audiplex.app.ui.player

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Decision logic for fallback mini-player visibility (#3505).
 *
 * When the MediaController reports playing music but the app's local state
 * (currentBook/currentMusic/currentStreamTitle) is null, the mini-player
 * should still show minimal controls using the controller's metadata.
 * This test pins the decision rule.
 */
class NowPlayingVisibilityTest {

    @Test
    fun `local state present always hides fallback`() {
        // If we have local state, use it (don't show fallback)
        assertEquals(false, NowPlayingVisibility.shouldShowFallback(
            hasLocalState = true,
            controllerHasMetadata = true,
            isPlaying = true
        ))
    }

    @Test
    fun `local state present with paused controller still hides fallback`() {
        assertEquals(false, NowPlayingVisibility.shouldShowFallback(
            hasLocalState = true,
            controllerHasMetadata = true,
            isPlaying = false
        ))
    }

    @Test
    fun `controller metadata with local state always hides fallback`() {
        assertEquals(false, NowPlayingVisibility.shouldShowFallback(
            hasLocalState = true,
            controllerHasMetadata = true,
            isPlaying = true
        ))
    }

    @Test
    fun `no local state and no controller metadata never shows fallback`() {
        assertEquals(false, NowPlayingVisibility.shouldShowFallback(
            hasLocalState = false,
            controllerHasMetadata = false,
            isPlaying = true
        ))
    }

    @Test
    fun `no local state with paused controller and no metadata never shows fallback`() {
        assertEquals(false, NowPlayingVisibility.shouldShowFallback(
            hasLocalState = false,
            controllerHasMetadata = false,
            isPlaying = false
        ))
    }

    @Test
    fun `no local state but controller playing shows fallback`() {
        // DJ started music; local state hasn't hydrated yet; show fallback
        assertEquals(true, NowPlayingVisibility.shouldShowFallback(
            hasLocalState = false,
            controllerHasMetadata = true,
            isPlaying = true
        ))
    }

    @Test
    fun `no local state but controller paused with metadata shows fallback`() {
        // DJ started music, then user paused before app hydrated; still show controls
        assertEquals(true, NowPlayingVisibility.shouldShowFallback(
            hasLocalState = false,
            controllerHasMetadata = true,
            isPlaying = false
        ))
    }

    @Test
    fun `no metadata and playing never shows fallback`() {
        // isPlaying=true but controller has no metadata: nothing to display
        assertEquals(false, NowPlayingVisibility.shouldShowFallback(
            hasLocalState = false,
            controllerHasMetadata = false,
            isPlaying = true
        ))
    }
}
