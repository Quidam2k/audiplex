package com.audiplex.app.playback

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.audiplex.app.data.SettingsStore
import dagger.hilt.android.AndroidEntryPoint
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull  // #5047
import javax.inject.Inject

/**
 * Brings the DJ link back up after a reboot (#3022).
 *
 * Without this, "always-on" quietly means "always-on until the phone
 * restarts, and then dead until Todd happens to open the app" — which is the
 * same silent non-delivery as 2026-08-14 wearing a different hat. BOOT_COMPLETED
 * is one of the states Android still permits a foreground-service start from,
 * so this is the one place the link can legitimately restart itself.
 */
@AndroidEntryPoint
class BootReceiver : BroadcastReceiver() {

    @Inject lateinit var settingsStore: SettingsStore
    @Inject lateinit var clientLog: ClientLogReporter

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        // goAsync: reading the toggle is a DataStore hop, and a receiver that
        // returns first would have its process torn down mid-read.
        val pending = goAsync()
        val appContext = context.applicationContext
        CoroutineScope(SupervisorJob() + Dispatchers.IO).launch {
            try {
                val enabled = settingsStore.djLinkEnabled.first()
                if (enabled) {
                    DjLinkService.start(appContext)
                }
                // #5047: proves the reboot path ran (#4906), whether or not the link is on.
                // Sent inside goAsync's ~10 s window: with the link off nothing else keeps the process up.
                withTimeoutOrNull(BOOT_LOG_WAIT_MS) {
                    clientLog.sendWhenReady("info", "boot_link_start", "phone booted", mapOf("dj_link" to "$enabled"))
                }
            } finally {
                pending.finish()
            }
        }
    }
}

/** #5047: under goAsync's ~10 s receiver limit. */
private const val BOOT_LOG_WAIT_MS = 8_000L
