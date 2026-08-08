package com.audiplex.app.ui.meditations

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.api.BookSummary
import com.audiplex.app.data.download.CATEGORY_MEDITATION
import com.audiplex.app.data.download.DownloadRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import javax.inject.Inject

data class MeditationsUiState(
    val meditations: List<BookSummary> = emptyList(),
    val isLoading: Boolean = true,
    val isQueueing: Boolean = false,
    val error: String? = null
)

@HiltViewModel
class MeditationsViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val downloadRepository: DownloadRepository
) : ViewModel() {

    private val _uiState = MutableStateFlow(MeditationsUiState())
    val uiState: StateFlow<MeditationsUiState> = _uiState

    /** book id -> download status, so each row can show its own state. */
    val downloadStates: StateFlow<Map<Int, String>> =
        downloadRepository.observeAllDownloads()
            .map { list -> list.associate { it.bookId to it.status } }
            .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyMap())

    init {
        load()
    }

    fun load() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true, error = null)
            try {
                val api = apiHolder.api
                if (api == null) {
                    _uiState.value = _uiState.value.copy(
                        isLoading = false, error = "No server configured"
                    )
                    return@launch
                }
                val books = api.getBooks(category = CATEGORY_MEDITATION)
                _uiState.value = MeditationsUiState(meditations = books, isLoading = false)
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    error = e.message ?: "Failed to load meditations"
                )
            }
        }
    }

    /**
     * The one-tap bulk action: queue every meditation for download. Each one
     * still goes through the normal per-item WorkManager pipeline, so progress,
     * notifications, retry and the Downloads screen all behave as they already
     * do — this only removes the ten separate taps.
     */
    fun downloadAll() {
        val books = _uiState.value.meditations
        if (books.isEmpty() || _uiState.value.isQueueing) return

        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isQueueing = true, error = null)
            val baseUrl = apiHolder.baseUrl
            val api = apiHolder.api
            var failed = 0
            for (book in books) {
                try {
                    // startDownload needs the full detail (file size + metadata
                    // JSON cached for offline display), which the list endpoint
                    // does not carry.
                    val detail = api?.getBook(book.id) ?: continue
                    downloadRepository.startDownload(detail, baseUrl)
                } catch (_: Exception) {
                    failed++
                }
            }
            _uiState.value = _uiState.value.copy(
                isQueueing = false,
                error = if (failed > 0) "$failed of ${books.size} could not be queued" else null
            )
        }
    }

}
