package com.audiplex.app.ui.library

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.FavoritesStore
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.data.api.AuthorSchema
import com.audiplex.app.data.api.BookSummary
import com.audiplex.app.data.api.ProgressSchema
import com.audiplex.app.data.api.SeriesSchema
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch
import javax.inject.Inject

enum class LibraryFilterMode { All, Favorites, ToRead }

sealed class LibraryUiState {
    data object Loading : LibraryUiState()
    data class Success(
        val books: List<BookSummary>,
        val authors: List<AuthorSchema>,
        val series: List<SeriesSchema>,
        val inProgress: List<InProgressBook>,
        val filterAuthor: String? = null,
        val filterSeries: String? = null,
        val filterMode: LibraryFilterMode = LibraryFilterMode.All
    ) : LibraryUiState()
    data class Error(val message: String) : LibraryUiState()
    data object NoServer : LibraryUiState()
}

data class InProgressBook(
    val book: BookSummary,
    val progress: ProgressSchema
)

@HiltViewModel
class LibraryViewModel @Inject constructor(
    private val apiHolder: ApiServiceHolder,
    private val settingsStore: SettingsStore,
    val favoritesStore: FavoritesStore
) : ViewModel() {

    private val _uiState = MutableStateFlow<LibraryUiState>(LibraryUiState.Loading)
    val uiState: StateFlow<LibraryUiState> = _uiState

    private val _isRefreshing = MutableStateFlow(false)
    val isRefreshing: StateFlow<Boolean> = _isRefreshing

    val favoritesByType = favoritesStore.byType

    init {
        viewModelScope.launch {
            settingsStore.serverUrl.distinctUntilChanged().collect { url ->
                if (url.isBlank()) {
                    _uiState.value = LibraryUiState.NoServer
                } else {
                    apiHolder.setBaseUrl(url)
                    loadLibrary()
                }
            }
        }
    }

    fun loadLibrary(
        filterAuthor: String? = null,
        filterSeries: String? = null,
        filterMode: LibraryFilterMode = LibraryFilterMode.All
    ) {
        viewModelScope.launch {
            _uiState.value = LibraryUiState.Loading
            doLoad(filterAuthor, filterSeries, filterMode)
        }
    }

    private suspend fun doLoad(
        filterAuthor: String? = null,
        filterSeries: String? = null,
        filterMode: LibraryFilterMode = LibraryFilterMode.All
    ) {
        try {
            val api = apiHolder.api ?: run {
                _uiState.value = LibraryUiState.NoServer
                return
            }
            val books = api.getBooks(
                author = filterAuthor,
                series = filterSeries
            )
            val authors = api.getAuthors()
            val series = api.getSeries()
            val progressList = try { api.getAllProgress() } catch (_: Exception) { emptyList() }

            val progressMap = progressList.associateBy { it.bookId }
            val allBooks = if (filterAuthor != null || filterSeries != null) {
                try { api.getBooks() } catch (_: Exception) { books }
            } else books

            val inProgress = allBooks
                .mapNotNull { book ->
                    progressMap[book.id]?.let { progress ->
                        if (!progress.isFinished) InProgressBook(book, progress) else null
                    }
                }
                .sortedByDescending { it.progress.updatedAt }

            _uiState.value = LibraryUiState.Success(
                books = books,
                authors = authors,
                series = series,
                inProgress = inProgress,
                filterAuthor = filterAuthor,
                filterSeries = filterSeries,
                filterMode = filterMode
            )
            favoritesStore.refresh()
        } catch (e: Exception) {
            _uiState.value = LibraryUiState.Error(e.message ?: "Failed to load library")
        }
    }

    fun refreshLibrary() {
        val current = _uiState.value as? LibraryUiState.Success
        viewModelScope.launch {
            _isRefreshing.value = true
            try {
                apiHolder.api?.scanLibrary()
            } catch (_: Exception) { }
            doLoad(current?.filterAuthor, current?.filterSeries, current?.filterMode ?: LibraryFilterMode.All)
            _isRefreshing.value = false
        }
    }

    fun clearFilter() {
        loadLibrary()
    }

    fun setFilterMode(mode: LibraryFilterMode) {
        val current = (_uiState.value as? LibraryUiState.Success) ?: return
        _uiState.value = current.copy(filterMode = mode)
    }

    fun filterByAuthor(author: String) {
        loadLibrary(filterAuthor = author)
    }

    fun filterBySeries(series: String) {
        loadLibrary(filterSeries = series)
    }

    fun toggleBookFavorite(bookId: Int) {
        viewModelScope.launch { favoritesStore.toggle("book", bookId.toString()) }
    }

    fun toggleBookToRead(bookId: Int) {
        viewModelScope.launch { favoritesStore.toggle("to_read", bookId.toString()) }
    }

    fun getBaseUrl(): String = apiHolder.baseUrl
}
