package com.audiplex.app.navigation

/**
 * #7044: Pantheon's home-screen volume icon opens Audiplex straight onto the Volumes screen
 * with this action. It only opens a screen (Jarvis, asg 15024: fine to export); nothing
 * here changes a level.
 */
object LevelsEntry {
    const val ACTION_OPEN_LEVELS = "com.audiplex.app.OPEN_LEVELS"

    fun wantsLevels(action: String?): Boolean = action == ACTION_OPEN_LEVELS
}
