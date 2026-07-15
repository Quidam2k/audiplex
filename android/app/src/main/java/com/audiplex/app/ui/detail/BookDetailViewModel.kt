package com.audiplex.app.ui.detail

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.api.BookDetail
import com.audiplex.app.data.db.DownloadEntity
import com.audiplex.app.data.download.DownloadRepository
import com.audiplex.app.data.download.PlaybackPositionRepository
import com.audiplex.app.data.download.ResumeInfo
import com.audiplex.app.playback.PlaybackManager
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import javax.inject.Inject

sealed class BookDetailUiState {
    data object Loading : BookDetailUiState()
    data class Success(
        val book: BookDetail,
        val resume: ResumeInfo
    ) : BookDetailUiState()
    data class Error(val message: String) : BookDetailUiState()
}

@HiltViewModel
class BookDetailViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val playbackManager: PlaybackManager,
    private val downloadRepository: DownloadRepository,
    private val playbackPositionRepository: PlaybackPositionRepository
) : ViewModel() {

    private val _uiState = MutableStateFlow<BookDetailUiState>(BookDetailUiState.Loading)
    val uiState: StateFlow<BookDetailUiState> = _uiState

    private val _currentBookId = MutableStateFlow(-1)

    @OptIn(ExperimentalCoroutinesApi::class)
    val downloadState: StateFlow<DownloadEntity?> = _currentBookId
        .flatMapLatest { id ->
            if (id < 0) flowOf(null) else downloadRepository.observeDownload(id)
        }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), null)

    fun loadBook(bookId: Int) {
        _currentBookId.value = bookId
        viewModelScope.launch {
            _uiState.value = BookDetailUiState.Loading
            try {
                val api = apiHolder.api
                // Offline-proof: fall back to cached download metadata if the
                // network fetch fails (or no server is configured).
                val book = try {
                    api?.getBook(bookId) ?: throw IllegalStateException("No server configured")
                } catch (e: Exception) {
                    downloadRepository.getCachedBookDetail(bookId) ?: throw e
                }
                // Resume target comes from the local-first reconciler, not the
                // raw server progress — so Continue is correct even offline.
                val resume = playbackPositionRepository.resolveStartPosition(bookId)
                _uiState.value = BookDetailUiState.Success(book, resume)
            } catch (e: Exception) {
                _uiState.value = BookDetailUiState.Error(e.message ?: "Failed to load book")
            }
        }
    }

    fun play(book: BookDetail, startSeconds: Double = 0.0) {
        playbackManager.play(book, apiHolder.baseUrl, startSeconds)
    }

    fun resume(book: BookDetail, resume: ResumeInfo) {
        playbackManager.play(book, apiHolder.baseUrl, resume.positionSeconds)
    }

    fun playFromChapter(book: BookDetail, chapterIndex: Int) {
        val chapter = book.chapters.getOrNull(chapterIndex) ?: return
        playbackManager.play(book, apiHolder.baseUrl, chapter.startSeconds)
    }

    fun startDownload(book: BookDetail) {
        viewModelScope.launch {
            downloadRepository.startDownload(book, apiHolder.baseUrl)
        }
    }

    fun pauseDownload() {
        val bookId = _currentBookId.value
        if (bookId < 0) return
        viewModelScope.launch { downloadRepository.pauseDownload(bookId) }
    }

    fun resumeDownload() {
        val bookId = _currentBookId.value
        if (bookId < 0) return
        viewModelScope.launch { downloadRepository.resumeDownload(bookId, apiHolder.baseUrl) }
    }

    fun deleteDownload() {
        val bookId = _currentBookId.value
        if (bookId < 0) return
        viewModelScope.launch { downloadRepository.deleteDownload(bookId) }
    }

    fun getBaseUrl(): String = apiHolder.baseUrl
}
