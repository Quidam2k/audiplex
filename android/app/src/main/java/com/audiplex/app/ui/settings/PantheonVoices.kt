package com.audiplex.app.ui.settings

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent

/**
 * #5422: one screen for every level. Agent voices stay on the Pantheon app's own slider
 * (Todd 53037, "sliders per app"), so Levels ends with a row that opens that app. Pantheon
 * has no intent for its gear sheet yet, so it opens to its home screen and the gear holds
 * the slider; the row says so.
 */
internal object PantheonVoices {
    const val PACKAGE = "com.pantheon.app"

    fun rowText(installed: Boolean): String =
        if (installed) "In the Pantheon app: tap the gear."
        else "Set in the Pantheon app, which isn't on this phone."

    fun launchIntent(context: Context): Intent? =
        context.packageManager.getLaunchIntentForPackage(PACKAGE)
            ?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

    /** Resolved at tap time: the app may have gone since the screen was drawn. */
    fun open(context: Context): Boolean {
        val intent = launchIntent(context) ?: return false
        return try {
            context.startActivity(intent)
            true
        } catch (e: ActivityNotFoundException) {
            false
        }
    }
}
