package com.audiplex.app.ui.common

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.api.FileInfo
import com.audiplex.app.data.download.DownloadRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

/** #5485: what the long-press sheet needs from the server: where the file lives, and a book download. */
@HiltViewModel
class ItemSheetViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val downloadRepository: DownloadRepository
) : ViewModel() {

    private val _file = MutableStateFlow<Pair<String, FileInfo>?>(null)
    /**
     * The last file looked up, tagged with its "kind:id" key. The sheet shows it only when the key
     * matches the item on screen, so one item's path never shows on another's sheet. Never set for
     * anyone but an admin (the server answers 403).
     */
    val file: StateFlow<Pair<String, FileInfo>?> = _file
    private var requested: String? = null

    fun load(kind: String, id: Int) {
        val k = key(kind, id)
        if (k == requested) return
        requested = k
        viewModelScope.launch {
            val info = try { apiHolder.api?.getFileInfo(kind, id) } catch (_: Exception) { null }
            if (info != null && requested == k) _file.value = k to info
        }
    }

    fun downloadBook(id: Int) {
        viewModelScope.launch {
            try {
                val api = apiHolder.api ?: return@launch
                downloadRepository.startDownload(api.getBook(id), apiHolder.baseUrl)
            } catch (_: Exception) { }
        }
    }

    companion object {
        fun key(kind: String, id: Int) = "$kind:$id"

        /** The /api/files kind for a sheet, or null where there is no single file (artist, genre, playlist). */
        fun fileKind(kind: ItemKind): String? = when (kind) {
            ItemKind.Track -> "track"
            ItemKind.Album -> "album"
            ItemKind.Book -> "book"
            else -> null
        }

        /** The text Copy puts on the clipboard: the full path, or the folder for an album. */
        fun copyText(info: FileInfo): String =
            info.fileName?.let { name ->
                val sep = if (info.folder.contains('\\')) "\\" else "/"
                if (info.folder.endsWith(sep)) info.folder + name else info.folder + sep + name
            } ?: info.folder
    }
}
