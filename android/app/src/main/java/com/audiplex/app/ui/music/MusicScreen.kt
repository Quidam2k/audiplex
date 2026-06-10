package com.audiplex.app.ui.music

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.clickable
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
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.TrendingUp
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.MusicNote
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Star
import androidx.compose.material.icons.filled.StarBorder
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
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
import com.audiplex.app.data.api.AlbumSummary
import com.audiplex.app.data.api.AudiplexApi
import com.audiplex.app.data.api.FolderListing
import com.audiplex.app.data.api.FolderNode
import com.audiplex.app.data.api.GenreSchema
import com.audiplex.app.data.api.MusicArtistSchema
import com.audiplex.app.data.api.PlaylistSummary
import com.audiplex.app.ui.common.AddToPlaylistButton
import com.audiplex.app.ui.common.SearchTopBar
import com.audiplex.app.ui.common.ItemActionSheet
import com.audiplex.app.ui.common.ItemKind
import com.audiplex.app.ui.common.NewPlaylistDialog
import com.audiplex.app.ui.common.PlaylistPickerSheet

/** Pending entity for an open action — kind + server key + display title. */
private data class PendingItem(
    val kind: ItemKind,
    val entityType: String,
    val entityKey: String,
    val title: String,
    val subtitle: String? = null
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MusicScreen(
    onGenreClick: (String) -> Unit,
    onArtistClick: (Int) -> Unit,
    onAlbumClick: (Int) -> Unit,
    onPlaylistClick: (Int) -> Unit,
    onMostPlayedClick: () -> Unit,
    onLikelySkipsClick: () -> Unit,
    onSettingsClick: () -> Unit,
    viewModel: MusicViewModel = hiltViewModel()
) {
    val uiState by viewModel.uiState.collectAsState()
    val favoritesByType by viewModel.favoritesByType.collectAsState()
    val favoritesOnly by viewModel.favoritesOnly.collectAsState()
    val isRefreshing by viewModel.isRefreshing.collectAsState()
    val folderListing by viewModel.folderListing.collectAsState()

    var pendingItem by remember { mutableStateOf<PendingItem?>(null) }
    var pickerForItem by remember { mutableStateOf<PendingItem?>(null) }
    var newPlaylistForItem by remember { mutableStateOf<PendingItem?>(null) }
    var searchQuery by rememberSaveable { mutableStateOf("") }

    Column(modifier = Modifier.fillMaxSize()) {
        SearchTopBar(
            title = "Music",
            searchQuery = searchQuery,
            onSearchQueryChange = { searchQuery = it },
            actions = {
                IconButton(onClick = { viewModel.setFavoritesOnly(!favoritesOnly) }) {
                    Icon(
                        if (favoritesOnly) Icons.Default.Star else Icons.Default.StarBorder,
                        contentDescription = "Toggle favorites filter",
                        tint = if (favoritesOnly)
                            MaterialTheme.colorScheme.primary
                        else
                            MaterialTheme.colorScheme.onSurface
                    )
                }
                IconButton(onClick = onSettingsClick) {
                    Icon(Icons.Default.Settings, contentDescription = "Settings")
                }
            }
        )

        when (val state = uiState) {
            is MusicUiState.Loading -> CenteredLoading()
            is MusicUiState.NoServer -> CenteredText("No server configured")
            is MusicUiState.Error -> CenteredText("Error: ${state.message}")
            is MusicUiState.Success -> PullToRefreshBox(
                isRefreshing = isRefreshing,
                onRefresh = { viewModel.refreshLibrary() },
                modifier = Modifier.fillMaxSize()
            ) {
                Column(Modifier.fillMaxSize()) {
                    MusicContent(
                        state = state,
                        baseUrl = viewModel.getBaseUrl(),
                        favoritesByType = favoritesByType,
                        favoritesOnly = favoritesOnly,
                        folderListing = folderListing,
                        query = searchQuery,
                        onGenreClick = onGenreClick,
                        onArtistClick = onArtistClick,
                        onAlbumClick = onAlbumClick,
                        onPlaylistClick = onPlaylistClick,
                        onMostPlayedClick = onMostPlayedClick,
                        onLikelySkipsClick = onLikelySkipsClick,
                        onOpenFolderRoot = { viewModel.openFolderRoot() },
                        onEnterFolder = { viewModel.enterFolder(it) },
                        onExitFolder = { viewModel.exitFolder() },
                        onLongPress = { pendingItem = it }
                    )
                }
            }
        }
    }

    pendingItem?.let { item ->
        val isFav = favoritesByType[item.entityType].orEmpty().contains(item.entityKey)
        ItemActionSheet(
            title = item.title,
            subtitle = item.subtitle,
            kind = item.kind,
            isFavorited = isFav,
            onDismiss = { pendingItem = null },
            onToggleFavorite = { viewModel.toggleFavorite(item.entityType, item.entityKey) },
            onAddToPlaylist = if (item.kind != ItemKind.Playlist) {
                { pickerForItem = item }
            } else null,
            onCreatePlaylist = if (item.kind != ItemKind.Playlist) {
                { newPlaylistForItem = item }
            } else null
        )
    }

    pickerForItem?.let { item ->
        val state = (uiState as? MusicUiState.Success) ?: return@let
        PlaylistPickerSheet(
            playlists = state.playlists,
            onDismiss = { pickerForItem = null },
            onPick = { playlist ->
                viewModel.addEntityToPlaylist(item.entityType, item.entityKey, playlist.id)
            },
            onCreateNew = { newPlaylistForItem = item }
        )
    }

    newPlaylistForItem?.let { item ->
        NewPlaylistDialog(
            suggestedName = item.title,
            onDismiss = { newPlaylistForItem = null },
            onConfirm = { name ->
                viewModel.createPlaylistFromEntity(item.entityType, item.entityKey, name)
                newPlaylistForItem = null
            }
        )
    }
}

@Composable
private fun MusicContent(
    state: MusicUiState.Success,
    baseUrl: String,
    favoritesByType: Map<String, Set<String>>,
    favoritesOnly: Boolean,
    folderListing: FolderListing?,
    query: String,
    onGenreClick: (String) -> Unit,
    onArtistClick: (Int) -> Unit,
    onAlbumClick: (Int) -> Unit,
    onPlaylistClick: (Int) -> Unit,
    onMostPlayedClick: () -> Unit,
    onLikelySkipsClick: () -> Unit,
    onOpenFolderRoot: () -> Unit,
    onEnterFolder: (String) -> Unit,
    onExitFolder: () -> Unit,
    onLongPress: (PendingItem) -> Unit
) {
    var selectedTab by rememberSaveable { mutableIntStateOf(0) }
    val tabs = listOf("Genres", "Artists", "Albums", "Folders", "Playlists")

    // Narrow each list by name before the per-tab favorites filtering runs.
    val genres = state.genres.filter { it.name.contains(query, ignoreCase = true) }
    val artists = state.artists.filter { it.name.contains(query, ignoreCase = true) }
    val albums = state.albums.filter { it.title.contains(query, ignoreCase = true) }
    val playlists = state.playlists.filter { it.name.contains(query, ignoreCase = true) }

    TabRow(
        selectedTabIndex = selectedTab,
        containerColor = MaterialTheme.colorScheme.surface,
        contentColor = MaterialTheme.colorScheme.primary
    ) {
        tabs.forEachIndexed { index, title ->
            Tab(
                selected = selectedTab == index,
                onClick = { selectedTab = index },
                text = {
                    Text(
                        title,
                        style = MaterialTheme.typography.labelMedium,
                        maxLines = 1,
                        softWrap = false,
                        overflow = TextOverflow.Ellipsis
                    )
                }
            )
        }
    }

    when (selectedTab) {
        0 -> GenresTab(
            genres = genres,
            favorites = favoritesByType["genre"].orEmpty(),
            favoritesOnly = favoritesOnly,
            onClick = onGenreClick,
            onLongClick = { g ->
                onLongPress(
                    PendingItem(
                        kind = ItemKind.Genre,
                        entityType = "genre",
                        entityKey = g.name,
                        title = g.name,
                        subtitle = "${g.albumCount} albums"
                    )
                )
            }
        )
        1 -> ArtistsTab(
            artists = artists,
            favorites = favoritesByType["artist"].orEmpty(),
            favoritesOnly = favoritesOnly,
            onClick = onArtistClick,
            onLongClick = { a ->
                onLongPress(
                    PendingItem(
                        kind = ItemKind.Artist,
                        entityType = "artist",
                        entityKey = a.id.toString(),
                        title = a.name,
                        subtitle = null
                    )
                )
            }
        )
        2 -> AlbumsGrid(
            albums = albums,
            baseUrl = baseUrl,
            favorites = favoritesByType["album"].orEmpty(),
            favoritesOnly = favoritesOnly,
            onClick = onAlbumClick,
            onLongClick = { album ->
                onLongPress(
                    PendingItem(
                        kind = ItemKind.Album,
                        entityType = "album",
                        entityKey = album.id.toString(),
                        title = album.title,
                        subtitle = album.artistName
                    )
                )
            }
        )
        3 -> FoldersTab(
            listing = folderListing,
            baseUrl = baseUrl,
            query = query,
            onOpenRoot = onOpenFolderRoot,
            onEnterFolder = onEnterFolder,
            onExitFolder = onExitFolder,
            onAlbumClick = onAlbumClick
        )
        4 -> PlaylistsTab(
            playlists = playlists,
            onClick = onPlaylistClick,
            onMostPlayedClick = onMostPlayedClick,
            onLikelySkipsClick = onLikelySkipsClick,
            onLongClick = { p ->
                onLongPress(
                    PendingItem(
                        kind = ItemKind.Playlist,
                        entityType = "playlist",
                        entityKey = p.id.toString(),
                        title = p.name,
                        subtitle = "${p.trackCount} tracks"
                    )
                )
            }
        )
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun GenresTab(
    genres: List<GenreSchema>,
    favorites: Set<String>,
    favoritesOnly: Boolean,
    onClick: (String) -> Unit,
    onLongClick: (GenreSchema) -> Unit
) {
    val list = if (favoritesOnly) genres.filter { it.name in favorites } else genres
    if (list.isEmpty()) {
        CenteredText(if (favoritesOnly) "No favorited genres" else "No genres found")
        return
    }
    LazyColumn(
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp)
    ) {
        items(list) { genre ->
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .combinedClickable(
                        onClick = { onClick(genre.name) },
                        onLongClick = { onLongClick(genre) }
                    )
                    .padding(vertical = 12.dp, horizontal = 8.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Row(
                    modifier = Modifier.weight(1f),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    if (genre.name in favorites) {
                        Icon(
                            Icons.Default.Star,
                            contentDescription = "Favorite",
                            tint = MaterialTheme.colorScheme.primary,
                            modifier = Modifier.size(16.dp)
                        )
                        Spacer(Modifier.size(8.dp))
                    }
                    Text(text = genre.name, style = MaterialTheme.typography.bodyLarge)
                }
                Text(
                    text = "${genre.albumCount} albums",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun ArtistsTab(
    artists: List<MusicArtistSchema>,
    favorites: Set<String>,
    favoritesOnly: Boolean,
    onClick: (Int) -> Unit,
    onLongClick: (MusicArtistSchema) -> Unit
) {
    val list = if (favoritesOnly) artists.filter { it.id.toString() in favorites } else artists
    if (list.isEmpty()) {
        CenteredText(if (favoritesOnly) "No favorited artists" else "No artists found")
        return
    }
    LazyColumn(
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp)
    ) {
        items(list) { artist ->
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .combinedClickable(
                        onClick = { onClick(artist.id) },
                        onLongClick = { onLongClick(artist) }
                    )
                    .padding(vertical = 12.dp, horizontal = 8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                if (artist.id.toString() in favorites) {
                    Icon(
                        Icons.Default.Star,
                        contentDescription = "Favorite",
                        tint = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.size(16.dp)
                    )
                    Spacer(Modifier.size(8.dp))
                }
                Text(text = artist.name, style = MaterialTheme.typography.bodyLarge)
            }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun AlbumsGrid(
    albums: List<AlbumSummary>,
    baseUrl: String,
    favorites: Set<String> = emptySet(),
    favoritesOnly: Boolean = false,
    onClick: (Int) -> Unit,
    onLongClick: ((AlbumSummary) -> Unit)? = null
) {
    val list = if (favoritesOnly) albums.filter { it.id.toString() in favorites } else albums
    if (list.isEmpty()) {
        CenteredText(if (favoritesOnly) "No favorited albums" else "No albums found")
        return
    }
    LazyVerticalGrid(
        columns = GridCells.Adaptive(minSize = 110.dp),
        contentPadding = PaddingValues(12.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        items(list, key = { it.id }) { album ->
            AlbumGridItem(
                album = album,
                baseUrl = baseUrl,
                isFavorite = album.id.toString() in favorites,
                onClick = { onClick(album.id) },
                onLongClick = if (onLongClick != null) { { onLongClick(album) } } else null
            )
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun AlbumGridItem(
    album: AlbumSummary,
    baseUrl: String,
    isFavorite: Boolean,
    onClick: () -> Unit,
    onLongClick: (() -> Unit)?
) {
    val rowModifier = if (onLongClick != null) {
        Modifier.combinedClickable(onClick = onClick, onLongClick = onLongClick)
    } else {
        Modifier.clickable(onClick = onClick)
    }
    Column(
        modifier = rowModifier,
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Box(modifier = Modifier.fillMaxWidth().aspectRatio(1f)) {
            AlbumCoverImage(
                albumId = album.id,
                hasCover = album.hasCover,
                title = album.title,
                baseUrl = baseUrl,
                modifier = Modifier.fillMaxSize().clip(RoundedCornerShape(8.dp))
            )
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
        }
        Spacer(Modifier.height(4.dp))
        Text(
            text = album.title,
            style = MaterialTheme.typography.bodySmall,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            textAlign = TextAlign.Center,
            modifier = Modifier.fillMaxWidth()
        )
        album.artistName?.let {
            Text(
                text = it,
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
fun AlbumCoverImage(
    albumId: Int,
    hasCover: Boolean,
    title: String,
    baseUrl: String,
    modifier: Modifier = Modifier
) {
    if (hasCover) {
        AsyncImage(
            model = AudiplexApi.musicCoverUrl(baseUrl, albumId),
            contentDescription = title,
            contentScale = ContentScale.Crop,
            modifier = modifier
        )
    } else {
        Box(modifier = modifier, contentAlignment = Alignment.Center) {
            Icon(
                Icons.Default.MusicNote,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

@Composable
private fun FoldersTab(
    listing: FolderListing?,
    baseUrl: String,
    query: String,
    onOpenRoot: () -> Unit,
    onEnterFolder: (String) -> Unit,
    onExitFolder: () -> Unit,
    onAlbumClick: (Int) -> Unit
) {
    LaunchedEffect(Unit) {
        if (listing == null) onOpenRoot()
    }
    if (listing == null) {
        CenteredLoading()
        return
    }

    // Narrow the current listing only — folder search stays within the view.
    val folders = listing.folders.filter { it.name.contains(query, ignoreCase = true) }
    val albums = listing.albums.filter { it.title.contains(query, ignoreCase = true) }

    Column(Modifier.fillMaxSize()) {
        // "Up" affordance + current location, shown once we're below the roots.
        if (listing.path != null) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable(onClick = onExitFolder)
                    .padding(horizontal = 16.dp, vertical = 12.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(
                    Icons.AutoMirrored.Filled.ArrowBack,
                    contentDescription = "Up one folder",
                    tint = MaterialTheme.colorScheme.primary
                )
                Spacer(Modifier.width(12.dp))
                Text(
                    text = folderDisplayPath(listing.path),
                    style = MaterialTheme.typography.bodyMedium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
            HorizontalDivider(color = MaterialTheme.colorScheme.surfaceVariant)
        }

        if (folders.isEmpty() && albums.isEmpty()) {
            CenteredText("This folder is empty")
            return@Column
        }

        LazyVerticalGrid(
            columns = GridCells.Adaptive(minSize = 110.dp),
            contentPadding = PaddingValues(12.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            items(
                folders,
                key = { "folder:${it.path}" },
                span = { GridItemSpan(maxLineSpan) }
            ) { folder ->
                FolderRow(folder = folder, onEnter = { onEnterFolder(folder.path) })
            }
            if (albums.isNotEmpty()) {
                if (folders.isNotEmpty()) {
                    item(span = { GridItemSpan(maxLineSpan) }) {
                        Text(
                            text = "Albums",
                            style = MaterialTheme.typography.titleSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = 4.dp)
                        )
                    }
                }
                items(albums, key = { "album:${it.id}" }) { album ->
                    AlbumGridItem(
                        album = album,
                        baseUrl = baseUrl,
                        isFavorite = false,
                        onClick = { onAlbumClick(album.id) },
                        onLongClick = null
                    )
                }
            }
        }
    }
}

@Composable
private fun FolderRow(
    folder: FolderNode,
    onEnter: () -> Unit
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onEnter)
            .padding(vertical = 4.dp, horizontal = 4.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Icon(
            Icons.Default.Folder,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.primary
        )
        Spacer(Modifier.width(12.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = folder.name,
                style = MaterialTheme.typography.bodyLarge,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            Text(
                text = "${folder.albumCount} albums • ${folder.trackCount} tracks",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
        AddToPlaylistButton(
            entityType = "folder",
            entityKey = folder.path,
            suggestedName = folder.name
        )
    }
}

/** Show the trailing couple of path segments — enough to orient without the full absolute path. */
private fun folderDisplayPath(path: String): String {
    val segments = path.replace("\\", "/").trimEnd('/').split("/").filter { it.isNotEmpty() }
    return segments.takeLast(2).joinToString(" / ")
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun PlaylistsTab(
    playlists: List<PlaylistSummary>,
    onClick: (Int) -> Unit,
    onMostPlayedClick: () -> Unit,
    onLikelySkipsClick: () -> Unit,
    onLongClick: (PlaylistSummary) -> Unit
) {
    LazyColumn(
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp)
    ) {
        item {
            SmartPlaylistRow(
                icon = Icons.AutoMirrored.Filled.TrendingUp,
                title = "Most Played",
                subtitle = "Top tracks by listens",
                onClick = onMostPlayedClick
            )
        }
        item {
            SmartPlaylistRow(
                icon = Icons.Default.Warning,
                title = "Skip suspects",
                subtitle = "Tracks abandoned early",
                onClick = onLikelySkipsClick
            )
        }
        item {
            HorizontalDivider(
                modifier = Modifier.padding(vertical = 8.dp),
                color = MaterialTheme.colorScheme.surfaceVariant
            )
        }
        if (playlists.isEmpty()) {
            item {
                Text(
                    text = "No playlists yet — long-press an album, artist, genre or track to create one.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(vertical = 24.dp, horizontal = 8.dp)
                )
            }
        }
        items(playlists) { playlist ->
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .combinedClickable(
                        onClick = { onClick(playlist.id) },
                        onLongClick = { onLongClick(playlist) }
                    )
                    .padding(vertical = 12.dp, horizontal = 8.dp),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = playlist.name,
                    style = MaterialTheme.typography.bodyLarge,
                    modifier = Modifier.weight(1f)
                )
                Text(
                    text = "${playlist.trackCount} tracks",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}

@Composable
private fun SmartPlaylistRow(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    title: String,
    subtitle: String,
    onClick: () -> Unit
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(vertical = 12.dp, horizontal = 8.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Icon(
            icon,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.primary,
            modifier = Modifier.size(24.dp)
        )
        Spacer(Modifier.size(16.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

@Composable
private fun CenteredLoading() {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)
    }
}

@Composable
private fun CenteredText(text: String) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Text(
            text = text,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(32.dp)
        )
    }
}
