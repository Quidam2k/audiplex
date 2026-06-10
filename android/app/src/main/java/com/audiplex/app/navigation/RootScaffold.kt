package com.audiplex.app.navigation

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Book
import androidx.compose.material.icons.filled.MusicNote
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.NavHostController
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.audiplex.app.ui.player.MiniPlayer
import com.audiplex.app.ui.player.PlayerViewModel

private data class RootTab(
    val route: String,
    val label: String,
    val icon: ImageVector
)

private val rootTabs = listOf(
    RootTab(Routes.LIBRARY, "Audiobooks", Icons.Default.Book),
    RootTab(Routes.MUSIC_HOME, "Music", Icons.Default.MusicNote)
)

@Composable
fun RootScaffold() {
    val navController = rememberNavController()
    val playerViewModel: PlayerViewModel = hiltViewModel()
    val hasActive by playerViewModel.hasActiveBook.collectAsState()
    val backStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = backStackEntry?.destination?.route

    val showMiniPlayer = hasActive && currentRoute != Routes.PLAYER
    val showBottomBar = currentRoute in rootTabs.map { it.route }

    Scaffold(
        modifier = Modifier.fillMaxSize(),
        bottomBar = {
            if (showMiniPlayer) {
                MiniPlayer(
                    viewModel = playerViewModel,
                    onClick = { navController.navigate(Routes.PLAYER) }
                )
            }
            if (showBottomBar) {
                BottomNav(navController = navController, currentRoute = currentRoute)
            }
        }
    ) { innerPadding ->
        Box(modifier = Modifier.padding(innerPadding)) {
            AppNavigation(navController = navController)
        }
    }
}

@Composable
private fun BottomNav(navController: NavHostController, currentRoute: String?) {
    NavigationBar {
        rootTabs.forEach { tab ->
            NavigationBarItem(
                selected = currentRoute == tab.route,
                onClick = {
                    if (currentRoute == tab.route) return@NavigationBarItem
                    navController.navigate(tab.route) {
                        popUpTo(navController.graph.findStartDestination().id) {
                            saveState = true
                        }
                        launchSingleTop = true
                        restoreState = true
                    }
                },
                icon = { Icon(tab.icon, contentDescription = tab.label) },
                label = { Text(tab.label) }
            )
        }
    }
}
