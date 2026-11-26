package com.audiplex.app.navigation

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** #7044 (Todd 68920): every volume slider one tap away, from Audiplex and from Pantheon. */
class LevelsEntry7044Test {
    private fun source(rel: String) = File("src/main/$rel").readText()

    @Test fun openLevelsActionIsTheOnlyOneThatOpensTheScreen() {
        assertTrue(LevelsEntry.wantsLevels("com.audiplex.app.OPEN_LEVELS"))
        assertFalse(LevelsEntry.wantsLevels("android.intent.action.MAIN"))
        assertFalse(LevelsEntry.wantsLevels(null))
    }

    @Test fun levelsHasItsOwnRouteWithTheSideBar() {
        assertEquals("levels", Routes.LEVELS)
        assertTrue(showsSideBar(Routes.LEVELS, libraryOnStack = true))
    }

    @Test fun manifestExportsTheOpenAction() {
        val manifest = source("AndroidManifest.xml")
        assertTrue(manifest.contains("<action android:name=\"${LevelsEntry.ACTION_OPEN_LEVELS}\" />"))
    }

    @Test fun theVolumesIconRidesBesideEveryGear() {
        // Every top bar drops in SettingsAction() (#4221); the Volumes icon is emitted from it.
        val action = source("java/com/audiplex/app/ui/common/SettingsAction.kt")
        val body = action.substringAfter("fun SettingsAction()")
        assertTrue(body.trimStart().removePrefix("{").trimStart().startsWith("LevelsAction()"))
    }

    @Test fun settingsNoLongerBuriesTheSliders() {
        val settings = source("java/com/audiplex/app/ui/settings/SettingsScreen.kt")
        val screen = settings.substringAfter("fun SettingsScreen(").substringBefore("internal fun LevelsSection(")
        assertFalse(screen.contains("LevelsSection("))
        assertTrue(source("java/com/audiplex/app/ui/settings/LevelsScreen.kt").contains("LevelsSection("))
    }
}
