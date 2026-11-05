package com.audiplex.app.ui.common

import com.audiplex.app.data.api.FileInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** #5485: the pure parts of the long-press sheet. */
class ItemSheetViewModelTest {

    @Test
    fun onlyBooksTracksAndAlbumsHaveAFile() {
        assertEquals("book", ItemSheetViewModel.fileKind(ItemKind.Book))
        assertEquals("track", ItemSheetViewModel.fileKind(ItemKind.Track))
        assertEquals("album", ItemSheetViewModel.fileKind(ItemKind.Album))
        assertNull(ItemSheetViewModel.fileKind(ItemKind.Artist))
        assertNull(ItemSheetViewModel.fileKind(ItemKind.Playlist))
    }

    @Test
    fun copyJoinsWindowsAndSlashPaths() {
        assertEquals("med\\audio\\Calm.m4a", ItemSheetViewModel.copyText(FileInfo("med\\audio", "Calm.m4a")))
        assertEquals("/music/A/01.mp3", ItemSheetViewModel.copyText(FileInfo("/music/A", "01.mp3")))
        assertEquals("/music/A", ItemSheetViewModel.copyText(FileInfo("/music/A", null)))
    }
}
