package com.audiplex.app.navigation

import android.net.Uri
import androidx.compose.runtime.Composable
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.navArgument
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.LaunchedEffect
import androidx.hilt.navigation.compose.hiltViewModel
import com.audiplex.app.ui.auth.LoginScreen
import com.audiplex.app.ui.auth.LoginViewModel
import com.audiplex.app.ui.detail.BookDetailScreen
import com.audiplex.app.ui.downloads.DownloadsScreen
import com.audiplex.app.ui.library.LibraryScreen
import com.audiplex.app.ui.music.AlbumDetailScreen
import com.audiplex.app.ui.music.AlbumsScreen
import com.audiplex.app.ui.music.ArtistDetailScreen
import com.audiplex.app.ui.music.LikelySkipsScreen
import com.audiplex.app.ui.music.MostPlayedScreen
import com.audiplex.app.ui.music.MusicScreen
import com.audiplex.app.ui.music.PlaylistDetailScreen
import com.audiplex.app.ui.player.PlayerScreen
import com.audiplex.app.ui.settings.SettingsScreen

object Routes {
    const val LOGIN = "login"
    const val LIBRARY = "library"
    const val BOOK_DETAIL = "book/{bookId}"
    const val PLAYER = "player"
    const val SETTINGS = "settings"
    const val DOWNLOADS = "downloads"

    const val MUSIC_HOME = "music"
    const val MUSIC_ARTIST = "music/artist/{artistId}"
    const val MUSIC_ALBUMS_FILTERED = "music/albums?genre={genre}&artistId={artistId}&title={title}"
    const val MUSIC_ALBUM = "music/album/{albumId}"
    const val MUSIC_PLAYLIST = "music/playlist/{playlistId}"
    const val MUSIC_MOST_PLAYED = "music/most-played"
    const val MUSIC_LIKELY_SKIPS = "music/likely-skips"

    fun bookDetail(bookId: Int) = "book/$bookId"
    fun musicArtist(artistId: Int) = "music/artist/$artistId"
    fun musicAlbumsByGenre(genre: String) =
        "music/albums?genre=${Uri.encode(genre)}&artistId=&title=${Uri.encode(genre)}"
    fun musicAlbum(albumId: Int) = "music/album/$albumId"
    fun musicPlaylist(playlistId: Int) = "music/playlist/$playlistId"
}

@Composable
fun AppNavigation(navController: NavHostController) {
    val loginViewModel: LoginViewModel = hiltViewModel()
    val isLoggedIn by loginViewModel.isLoggedIn.collectAsState()

    LaunchedEffect(isLoggedIn) {
        if (isLoggedIn == false) {
            navController.navigate(Routes.LOGIN) {
                popUpTo(0) { inclusive = true }
            }
        }
    }

    NavHost(
        navController = navController,
        startDestination = Routes.LIBRARY
    ) {
        composable(Routes.LOGIN) {
            LoginScreen(
                onLoginSuccess = {
                    navController.navigate(Routes.LIBRARY) {
                        popUpTo(Routes.LOGIN) { inclusive = true }
                    }
                }
            )
        }

        composable(Routes.LIBRARY) {
            LibraryScreen(
                onBookClick = { bookId -> navController.navigate(Routes.bookDetail(bookId)) },
                onSettingsClick = { navController.navigate(Routes.SETTINGS) },
                onPlayerClick = { navController.navigate(Routes.PLAYER) }
            )
        }

        composable(
            route = Routes.BOOK_DETAIL,
            arguments = listOf(navArgument("bookId") { type = NavType.IntType })
        ) { backStackEntry ->
            val bookId = backStackEntry.arguments?.getInt("bookId") ?: return@composable
            BookDetailScreen(
                bookId = bookId,
                onBack = { navController.popBackStack() },
                onPlayerClick = { navController.navigate(Routes.PLAYER) }
            )
        }

        composable(Routes.PLAYER) {
            PlayerScreen(
                onBack = { navController.popBackStack() }
            )
        }

        composable(Routes.SETTINGS) {
            SettingsScreen(
                onBack = { navController.popBackStack() },
                onDownloadsClick = { navController.navigate(Routes.DOWNLOADS) },
                onLogout = {
                    navController.navigate(Routes.LOGIN) {
                        popUpTo(0) { inclusive = true }
                    }
                }
            )
        }

        composable(Routes.DOWNLOADS) {
            DownloadsScreen(
                onBack = { navController.popBackStack() }
            )
        }

        composable(Routes.MUSIC_HOME) {
            MusicScreen(
                onGenreClick = { genre -> navController.navigate(Routes.musicAlbumsByGenre(genre)) },
                onArtistClick = { artistId -> navController.navigate(Routes.musicArtist(artistId)) },
                onAlbumClick = { albumId -> navController.navigate(Routes.musicAlbum(albumId)) },
                onPlaylistClick = { playlistId -> navController.navigate(Routes.musicPlaylist(playlistId)) },
                onMostPlayedClick = { navController.navigate(Routes.MUSIC_MOST_PLAYED) },
                onLikelySkipsClick = { navController.navigate(Routes.MUSIC_LIKELY_SKIPS) },
                onSettingsClick = { navController.navigate(Routes.SETTINGS) }
            )
        }

        composable(Routes.MUSIC_MOST_PLAYED) {
            MostPlayedScreen(
                onBack = { navController.popBackStack() },
                onPlayerClick = { navController.navigate(Routes.PLAYER) }
            )
        }

        composable(Routes.MUSIC_LIKELY_SKIPS) {
            LikelySkipsScreen(
                onBack = { navController.popBackStack() },
                onPlayerClick = { navController.navigate(Routes.PLAYER) }
            )
        }

        composable(
            route = Routes.MUSIC_ALBUMS_FILTERED,
            arguments = listOf(
                navArgument("genre") { type = NavType.StringType; nullable = true; defaultValue = null },
                navArgument("artistId") { type = NavType.StringType; nullable = true; defaultValue = null },
                navArgument("title") { type = NavType.StringType; defaultValue = "Albums" }
            )
        ) { backStackEntry ->
            val args = backStackEntry.arguments
            val rawGenre = args?.getString("genre")
            val genre = rawGenre?.takeIf { it.isNotEmpty() }
            val rawArtist = args?.getString("artistId")
            val artistId = rawArtist?.takeIf { it.isNotEmpty() }?.toIntOrNull()
            val title = args?.getString("title") ?: "Albums"
            AlbumsScreen(
                genre = genre,
                artistId = artistId,
                title = title,
                onBack = { navController.popBackStack() },
                onAlbumClick = { albumId -> navController.navigate(Routes.musicAlbum(albumId)) },
                onPlayerClick = { navController.navigate(Routes.PLAYER) }
            )
        }

        composable(
            route = Routes.MUSIC_ARTIST,
            arguments = listOf(navArgument("artistId") { type = NavType.IntType })
        ) { backStackEntry ->
            val artistId = backStackEntry.arguments?.getInt("artistId") ?: return@composable
            ArtistDetailScreen(
                artistId = artistId,
                onBack = { navController.popBackStack() },
                onAlbumClick = { albumId -> navController.navigate(Routes.musicAlbum(albumId)) },
                onPlayerClick = { navController.navigate(Routes.PLAYER) }
            )
        }

        composable(
            route = Routes.MUSIC_ALBUM,
            arguments = listOf(navArgument("albumId") { type = NavType.IntType })
        ) { backStackEntry ->
            val albumId = backStackEntry.arguments?.getInt("albumId") ?: return@composable
            AlbumDetailScreen(
                albumId = albumId,
                onBack = { navController.popBackStack() },
                onPlayerClick = { navController.navigate(Routes.PLAYER) }
            )
        }

        composable(
            route = Routes.MUSIC_PLAYLIST,
            arguments = listOf(navArgument("playlistId") { type = NavType.IntType })
        ) { backStackEntry ->
            val playlistId = backStackEntry.arguments?.getInt("playlistId") ?: return@composable
            PlaylistDetailScreen(
                playlistId = playlistId,
                onBack = { navController.popBackStack() },
                onPlayerClick = { navController.navigate(Routes.PLAYER) }
            )
        }
    }
}
