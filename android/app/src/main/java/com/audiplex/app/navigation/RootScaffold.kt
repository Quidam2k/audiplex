package com.audiplex.app.navigation

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column  // #6781
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Scaffold
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider  // #4221
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf  // #5396
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.navigation.NavHostController
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.audiplex.app.ui.common.LocalGoHome  // #5396
import com.audiplex.app.ui.common.LocalHomeTick
import com.audiplex.app.ui.common.LocalOpenSettings  // #4221
import com.audiplex.app.ui.common.LocalOpenLevels  // #7044
import androidx.compose.runtime.LaunchedEffect  // #7044
import com.audiplex.app.ui.player.MiniPlayer
import com.audiplex.app.ui.player.PlayerViewModel

@Composable
fun RootScaffold(openLevelsTick: Int = 0) {  // #7044: bumped by MainActivity on OPEN_LEVELS
    val navController = rememberNavController()
    val playerViewModel: PlayerViewModel = hiltViewModel()
    val hasActive by playerViewModel.hasActiveBook.collectAsState()
    val backStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = backStackEntry?.destination?.route

    val showMiniPlayer = hasActive && currentRoute != Routes.PLAYER
    // #6781: the Audiobooks / Music switch is on every screen (Player included), so Todd
    // hops sides without backing out.
    val (side, showBottomBar) = remember(backStackEntry) {
        fun onStack(route: String) = runCatching { navController.getBackStackEntry(route) }.isSuccess
        sideOf(onStack(Routes.MUSIC_HOME)) to showsSideBar(currentRoute, onStack(Routes.LIBRARY))
    }

    val openSettings = remember(navController) {  // #4221
        { navController.navigate(Routes.SETTINGS) { launchSingleTop = true } }
    }
    val openLevels = remember(navController) {  // #7044
        { navController.navigate(Routes.LEVELS) { launchSingleTop = true } }  // #7044
    }  // #7044
    LaunchedEffect(openLevelsTick) { if (openLevelsTick > 0) openLevels() }  // #7044
    var homeTick by rememberSaveable { mutableIntStateOf(0) }  // #5396
    val goHome = remember(navController) { { homeTick++; navController.goHome() } }
    CompositionLocalProvider(
        LocalOpenSettings provides openSettings,  // #4221
        LocalOpenLevels provides openLevels,  // #7044
        LocalGoHome provides goHome,  // #5396
        LocalHomeTick provides homeTick
    ) { Scaffold(
        modifier = Modifier.fillMaxSize(),
        bottomBar = {
            // #6781: one Column, so the mini-player sits above the bar instead of under it
            // (Scaffold's bottomBar slot overlaps its children).
            // Emitted only when it has content: an empty one would zero the bottom inset.
            if (showMiniPlayer || showBottomBar) Column {
                if (showMiniPlayer) {
                    MiniPlayer(
                        viewModel = playerViewModel,
                        onClick = { navController.openPlayer() }
                    )
                }
                if (showBottomBar) {
                    SideBar(selected = side, onTap = { navController.tapSide(currentRoute, side, it) })
                }
            }
        }
    ) { innerPadding ->
        Box(modifier = Modifier.padding(innerPadding)) {
            AppNavigation(navController = navController)
        }
    } }  // #4221
}

/**
 * #6781: tapping the side you are on returns to its root; tapping the other side switches.
 * Only Music's screens are saved and restored (it comes back inside the album you left).
 * Audiobooks comes back to the Library screen, which is never popped, so its tab and scroll
 * stay. Library is the start destination: saving screens under its id goes stale after Back,
 * Home or logout and reopens an old book, so Music is saved with an INCLUSIVE pop of its own
 * root, which files the state under Music home only.
 */
private fun NavHostController.tapSide(currentRoute: String?, currentSide: Side, tapped: Side) {
    when (sideTap(currentRoute, currentSide, tapped)) {
        SideTap.NOTHING -> Unit
        SideTap.POP_TO_ROOT -> popBackStack(tapped.root, inclusive = false)
        SideTap.SWITCH -> when (tapped) {
            Side.AUDIOBOOKS -> popBackStack(Routes.MUSIC_HOME, inclusive = true, saveState = true)
            Side.MUSIC -> navigate(Routes.MUSIC_HOME) {
                popUpTo(Routes.LIBRARY)
                launchSingleTop = true
                restoreState = true
            }
        }
    }
}
