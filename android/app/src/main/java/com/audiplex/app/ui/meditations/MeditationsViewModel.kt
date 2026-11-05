package com.audiplex.app.ui.meditations

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.api.BookSummary
import com.audiplex.app.data.download.CATEGORY_MEDITATION
import com.audiplex.app.data.download.DownloadRepository
import com.audiplex.app.data.download.MeditationSync
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import javax.inject.Inject

data class MeditationsUiState(
    /** #5465: newest first, sleep sounds taken out. */
    val meditations: List<BookSummary> = emptyList(),
    /** #5465: the server's sleep beds, shown in their own section. */
    val sleepSounds: List<BookSummary> = emptyList(),
    val isLoading: Boolean = true,
    val isQueueing: Boolean = false,
    val error: String? = null
)

@HiltViewModel
class MeditationsViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val downloadRepository: DownloadRepository,
    private val meditationSync: MeditationSync
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

    /** #9295: pick up meditation files added since the list was loaded. */
    fun refresh() {
        viewModelScope.launch {
            try { apiHolder.api?.scanMeditations() } catch (_: Exception) { }
            load()
        }
    }

    fun load() {
        viewModelScope.launch {
            // #10702: coming back to the tab refreshes quietly; the spinner is only for a first load.
            val current = _uiState.value
            val firstLoad = current.meditations.isEmpty() && current.sleepSounds.isEmpty()
            _uiState.value = current.copy(isLoading = firstLoad, error = null)
            try {
                val api = apiHolder.api
                if (api == null) {
                    _uiState.value = _uiState.value.copy(
                        isLoading = false, error = "No server configured"
                    )
                    return@launch
                }
                val books = api.getBooks(category = CATEGORY_MEDITATION)
                // #5465: if the bed list can't be fetched, show every row as a meditation rather than hide any.
                val bedIds = try { api.getSleepBeds().map { it.id }.toSet() } catch (_: Exception) { null }
                val sections = MeditationLists.split(books, bedIds)
                _uiState.value = MeditationsUiState(
                    meditations = sections.meditations,
                    sleepSounds = sections.sleepSounds,
                    isLoading = false
                )
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    error = e.message ?: "Failed to load meditations"
                )
            }
        }
    }

    /**
     * The one-tap bulk action: queue every meditation that isn't already on the phone (#845; sleep
     * sounds are left out, the bed keeps its own copy). Each one
     * still goes through the normal per-item WorkManager pipeline, so progress,
     * notifications, retry and the Downloads screen all behave as they already
     * do — this only removes the ten separate taps.
     */
    fun downloadAll() {
        val books = _uiState.value.meditations
        if (books.isEmpty() || _uiState.value.isQueueing) return

        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isQueueing = true, error = null)
            // #5462: the same pass the background auto-download runs, minus the Wi-Fi-only pin.
            val (queued, failed) = meditationSync.queueMissing(books, unmeteredOnly = false)
            _uiState.value = _uiState.value.copy(
                isQueueing = false,
                error = if (failed > 0) "$failed of ${failed + queued} could not be queued" else null
            )
        }
    }

}
