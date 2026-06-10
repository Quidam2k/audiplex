package com.audiplex.app.ui.library

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Book
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Star
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import coil3.compose.AsyncImage
import com.audiplex.app.data.api.AudiplexApi
import com.audiplex.app.data.api.BookSummary
import com.audiplex.app.ui.common.ItemActionSheet
import com.audiplex.app.ui.common.ItemKind

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LibraryScreen(
    onBookClick: (Int) -> Unit,
    onSettingsClick: () -> Unit,
    onPlayerClick: () -> Unit,
    viewModel: LibraryViewModel = hiltViewModel()
) {
    val uiState by viewModel.uiState.collectAsState()
    val favoritesByType by viewModel.favoritesByType.collectAsState()
    val isRefreshing by viewModel.isRefreshing.collectAsState()
    var pendingBook by remember { mutableStateOf<BookSummary?>(null) }

    Column(modifier = Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text("Audiplex") },
            colors = TopAppBarDefaults.topAppBarColors(
                containerColor = MaterialTheme.colorScheme.surface
            ),
            actions = {
                IconButton(onClick = onSettingsClick) {
                    Icon(Icons.Default.Settings, contentDescription = "Settings")
                }
            }
        )

        when (val state = uiState) {
            is LibraryUiState.Loading -> {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)
                }
            }

            is LibraryUiState.NoServer -> {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(
                            "No server configured",
                            style = MaterialTheme.typography.titleMedium,
                            color = MaterialTheme.colorScheme.onSurface
                        )
                        Spacer(Modifier.height(8.dp))
                        Text(
                            "Tap the gear icon to set your server URL",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            }

            is LibraryUiState.Error -> {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(
                            "Error loading library",
                            style = MaterialTheme.typography.titleMedium,
                            color = MaterialTheme.colorScheme.error
                        )
                        Spacer(Modifier.height(8.dp))
                        Text(
                            state.message,
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            textAlign = TextAlign.Center,
                            modifier = Modifier.padding(horizontal = 32.dp)
                        )
                    }
                }
            }

            is LibraryUiState.Success -> {
                LibraryContent(
                    state = state,
                    isRefreshing = isRefreshing,
                    favoriteBookIds = favoritesByType["book"].orEmpty(),
                    toReadBookIds = favoritesByType["to_read"].orEmpty(),
                    baseUrl = viewModel.getBaseUrl(),
                    onBookClick = onBookClick,
                    onBookLongPress = { pendingBook = it },
                    onAuthorClick = { viewModel.filterByAuthor(it) },
                    onSeriesClick = { viewModel.filterBySeries(it) },
                    onClearFilter = { viewModel.clearFilter() },
                    onFilterModeChange = { viewModel.setFilterMode(it) },
                    onRefresh = { viewModel.refreshLibrary() }
                )
            }
        }
    }

    pendingBook?.let { book ->
        val key = book.id.toString()
        ItemActionSheet(
            title = book.title,
            subtitle = book.author,
            kind = ItemKind.Book,
            isFavorited = key in favoritesByType["book"].orEmpty(),
            isInToRead = key in favoritesByType["to_read"].orEmpty(),
            onDismiss = { pendingBook = null },
            onToggleFavorite = { viewModel.toggleBookFavorite(book.id) },
            onToggleToRead = { viewModel.toggleBookToRead(book.id) }
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun LibraryContent(
    state: LibraryUiState.Success,
    isRefreshing: Boolean,
    favoriteBookIds: Set<String>,
    toReadBookIds: Set<String>,
    baseUrl: String,
    onBookClick: (Int) -> Unit,
    onBookLongPress: (BookSummary) -> Unit,
    onAuthorClick: (String) -> Unit,
    onSeriesClick: (String) -> Unit,
    onClearFilter: () -> Unit,
    onFilterModeChange: (LibraryFilterMode) -> Unit,
    onRefresh: () -> Unit
) {
    var selectedTab by remember { mutableIntStateOf(0) }
    val tabs = listOf("Books", "Authors", "Series")

    // Active author/series filter chip
    val activeFilter = state.filterAuthor ?: state.filterSeries
    if (activeFilter != null) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                text = if (state.filterAuthor != null) "Author: $activeFilter" else "Series: $activeFilter",
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.primary
            )
            Spacer(Modifier.width(8.dp))
            Text(
                text = "✕ Clear",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.clickable { onClearFilter() }
            )
        }
    }

    TabRow(
        selectedTabIndex = selectedTab,
        containerColor = MaterialTheme.colorScheme.surface,
        contentColor = MaterialTheme.colorScheme.primary
    ) {
        tabs.forEachIndexed { index, title ->
            Tab(
                selected = selectedTab == index,
                onClick = { selectedTab = index },
                text = { Text(title) }
            )
        }
    }

    // Books-tab-only: filter mode chips (All / Favorites / Want to read).
    if (selectedTab == 0) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            FilterChip(
                selected = state.filterMode == LibraryFilterMode.All,
                onClick = { onFilterModeChange(LibraryFilterMode.All) },
                label = { Text("All") }
            )
            FilterChip(
                selected = state.filterMode == LibraryFilterMode.Favorites,
                onClick = { onFilterModeChange(LibraryFilterMode.Favorites) },
                label = { Text("★ Favorites") },
                leadingIcon = if (state.filterMode == LibraryFilterMode.Favorites) {
                    { Icon(Icons.Default.Star, contentDescription = null, modifier = Modifier.size(FilterChipDefaults.IconSize)) }
                } else null
            )
            FilterChip(
                selected = state.filterMode == LibraryFilterMode.ToRead,
                onClick = { onFilterModeChange(LibraryFilterMode.ToRead) },
                label = { Text("Want to read") },
                leadingIcon = if (state.filterMode == LibraryFilterMode.ToRead) {
                    { Icon(Icons.Default.Bookmark, contentDescription = null, modifier = Modifier.size(FilterChipDefaults.IconSize)) }
                } else null
            )
        }
    }

    PullToRefreshBox(
        isRefreshing = isRefreshing,
        onRefresh = onRefresh,
        modifier = Modifier.fillMaxSize()
    ) {
        when (selectedTab) {
            0 -> {
                val filteredBooks = when (state.filterMode) {
                    LibraryFilterMode.All -> state.books
                    LibraryFilterMode.Favorites -> state.books.filter { it.id.toString() in favoriteBookIds }
                    LibraryFilterMode.ToRead -> state.books.filter { it.id.toString() in toReadBookIds }
                }
                BooksTab(
                    books = filteredBooks,
                    inProgress = if (state.filterMode == LibraryFilterMode.All) state.inProgress else emptyList(),
                    favoriteBookIds = favoriteBookIds,
                    toReadBookIds = toReadBookIds,
                    baseUrl = baseUrl,
                    onBookClick = onBookClick,
                    onBookLongPress = onBookLongPress,
                    emptyMessage = when (state.filterMode) {
                        LibraryFilterMode.All -> null
                        LibraryFilterMode.Favorites -> "No favorited books yet. Long-press a book cover to favorite it."
                        LibraryFilterMode.ToRead -> "Your Want to read list is empty. Long-press a book to add it."
                    }
                )
            }
            1 -> AuthorsTab(
                authors = state.authors,
                onAuthorClick = { author ->
                    onAuthorClick(author)
                    selectedTab = 0
                }
            )
            2 -> SeriesTab(
                series = state.series,
                onSeriesClick = { series ->
                    onSeriesClick(series)
                    selectedTab = 0
                }
            )
        }
    }
}

@Composable
private fun BooksTab(
    books: List<BookSummary>,
    inProgress: List<InProgressBook>,
    favoriteBookIds: Set<String>,
    toReadBookIds: Set<String>,
    baseUrl: String,
    onBookClick: (Int) -> Unit,
    onBookLongPress: (BookSummary) -> Unit,
    emptyMessage: String?
) {
    if (books.isEmpty() && inProgress.isEmpty() && emptyMessage != null) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text(
                text = emptyMessage,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(32.dp)
            )
        }
        return
    }
    LazyVerticalGrid(
        columns = GridCells.Adaptive(minSize = 110.dp),
        contentPadding = PaddingValues(12.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        if (inProgress.isNotEmpty()) {
            item(span = { GridItemSpan(maxLineSpan) }) {
                Column {
                    Text(
                        "Continue Listening",
                        style = MaterialTheme.typography.titleMedium,
                        color = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.padding(bottom = 8.dp)
                    )
                    LazyRow(
                        horizontalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        items(inProgress) { item ->
                            ContinueListeningCard(
                                book = item.book,
                                baseUrl = baseUrl,
                                onClick = { onBookClick(item.book.id) }
                            )
                        }
                    }
                    Spacer(Modifier.height(16.dp))
                    Text(
                        "All Books (${books.size})",
                        style = MaterialTheme.typography.titleMedium,
                        color = MaterialTheme.colorScheme.onSurface
                    )
                }
            }
        }

        items(books) { book ->
            BookGridItem(
                book = book,
                baseUrl = baseUrl,
                isFavorite = book.id.toString() in favoriteBookIds,
                isInToRead = book.id.toString() in toReadBookIds,
                onClick = { onBookClick(book.id) },
                onLongClick = { onBookLongPress(book) }
            )
        }
    }
}

@Composable
private fun ContinueListeningCard(
    book: BookSummary,
    baseUrl: String,
    onClick: () -> Unit
) {
    Card(
        modifier = Modifier
            .width(140.dp)
            .clickable(onClick = onClick),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
    ) {
        Column {
            BookCoverImage(
                book = book,
                baseUrl = baseUrl,
                modifier = Modifier
                    .fillMaxWidth()
                    .aspectRatio(1f)
                    .clip(RoundedCornerShape(topStart = 12.dp, topEnd = 12.dp))
            )
            Text(
                text = book.title,
                style = MaterialTheme.typography.bodySmall,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.padding(8.dp)
            )
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun BookGridItem(
    book: BookSummary,
    baseUrl: String,
    isFavorite: Boolean,
    isInToRead: Boolean,
    onClick: () -> Unit,
    onLongClick: () -> Unit
) {
    Column(
        modifier = Modifier.combinedClickable(onClick = onClick, onLongClick = onLongClick),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Box(modifier = Modifier.fillMaxWidth().aspectRatio(1f)) {
            BookCoverImage(
                book = book,
                baseUrl = baseUrl,
                modifier = Modifier.fillMaxSize().clip(RoundedCornerShape(8.dp))
            )
            // Overlap chips: star (favorite) top-end, bookmark (to-read)
            // top-start. Two ribbons make the state legible at a glance.
            if (isFavorite) {
                Icon(
                    Icons.Default.Star,
                    contentDescription = "Favorite",
                    tint = MaterialTheme.colorScheme.primary,
                    modifier = Modifier
                        .align(Alignment.TopEnd)
                        .padding(4.dp)
                        .size(20.dp)
                )
            }
            if (isInToRead) {
                Icon(
                    Icons.Default.Bookmark,
                    contentDescription = "Want to read",
                    tint = MaterialTheme.colorScheme.tertiary,
                    modifier = Modifier
                        .align(Alignment.TopStart)
                        .padding(4.dp)
                        .size(20.dp)
                )
            }
        }
        Spacer(Modifier.height(4.dp))
        Text(
            text = book.title,
            style = MaterialTheme.typography.bodySmall,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            textAlign = TextAlign.Center,
            modifier = Modifier.fillMaxWidth()
        )
        book.author?.let { author ->
            Text(
                text = author,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                textAlign = TextAlign.Center,
                modifier = Modifier.fillMaxWidth()
            )
        }
    }
}

@Composable
private fun BookCoverImage(
    book: BookSummary,
    baseUrl: String,
    modifier: Modifier = Modifier
) {
    if (book.hasCover) {
        AsyncImage(
            model = AudiplexApi.coverUrl(baseUrl, book.id),
            contentDescription = book.title,
            contentScale = ContentScale.Crop,
            modifier = modifier
        )
    } else {
        Box(
            modifier = modifier,
            contentAlignment = Alignment.Center
        ) {
            Icon(
                Icons.Default.Book,
                contentDescription = null,
                modifier = Modifier.size(48.dp),
                tint = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

@Composable
private fun AuthorsTab(
    authors: List<com.audiplex.app.data.api.AuthorSchema>,
    onAuthorClick: (String) -> Unit
) {
    LazyColumn(
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp)
    ) {
        items(authors) { author ->
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { onAuthorClick(author.name) }
                    .padding(vertical = 12.dp, horizontal = 8.dp),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = author.name,
                    style = MaterialTheme.typography.bodyLarge,
                    modifier = Modifier.weight(1f)
                )
                Text(
                    text = "${author.bookCount} books",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}

@Composable
private fun SeriesTab(
    series: List<com.audiplex.app.data.api.SeriesSchema>,
    onSeriesClick: (String) -> Unit
) {
    LazyColumn(
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp)
    ) {
        items(series) { s ->
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { onSeriesClick(s.name) }
                    .padding(vertical = 12.dp, horizontal = 8.dp),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = s.name,
                    style = MaterialTheme.typography.bodyLarge,
                    modifier = Modifier.weight(1f)
                )
                Text(
                    text = "${s.bookCount} books",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}
