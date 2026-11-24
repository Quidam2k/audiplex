package com.audiplex.app.navigation

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** #6781: the Audiobooks / Music switch on every screen. */
class SideNavTest {
    @Test fun sideFollowsMusicHomeOnTheStack() {
        assertEquals(Side.MUSIC, sideOf(musicOnStack = true))
        assertEquals(Side.AUDIOBOOKS, sideOf(musicOnStack = false))
    }

    @Test fun barIsEverywhereButLogin() {
        listOf(Routes.LIBRARY, Routes.PLAYER, Routes.SETTINGS, Routes.DOWNLOADS, Routes.MUSIC_ALBUM,
            Routes.BOOK_DETAIL).forEach { assertTrue(it, showsSideBar(it, libraryOnStack = true)) }
        assertFalse(showsSideBar(Routes.LOGIN, libraryOnStack = true))
        assertFalse(showsSideBar(null, libraryOnStack = true))
    }

    @Test fun noBarOnAPlayerOpenedFromLogin() {
        assertFalse(showsSideBar(Routes.PLAYER, libraryOnStack = false))
    }

    @Test fun tapResolution() {
        assertEquals(SideTap.NOTHING, sideTap(Routes.MUSIC_HOME, Side.MUSIC, Side.MUSIC))
        assertEquals(SideTap.NOTHING, sideTap(Routes.LIBRARY, Side.AUDIOBOOKS, Side.AUDIOBOOKS))
        assertEquals(SideTap.POP_TO_ROOT, sideTap(Routes.MUSIC_ALBUM, Side.MUSIC, Side.MUSIC))
        assertEquals(SideTap.POP_TO_ROOT, sideTap(Routes.PLAYER, Side.AUDIOBOOKS, Side.AUDIOBOOKS))
        assertEquals(SideTap.SWITCH, sideTap(Routes.PLAYER, Side.MUSIC, Side.AUDIOBOOKS))
        assertEquals(SideTap.SWITCH, sideTap(Routes.LIBRARY, Side.AUDIOBOOKS, Side.MUSIC))
    }
}
