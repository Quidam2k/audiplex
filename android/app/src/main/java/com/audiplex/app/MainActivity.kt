package com.audiplex.app

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.audiplex.app.data.SettingsStore
import com.audiplex.app.navigation.RootScaffold
import com.audiplex.app.playback.DjLinkService
import com.audiplex.app.ui.theme.AudiplexTheme
import dagger.hilt.android.AndroidEntryPoint
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import javax.inject.Inject

@AndroidEntryPoint
class MainActivity : ComponentActivity() {

    @Inject lateinit var settingsStore: SettingsStore

    private val requestNotifications =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        askForNotificationsIfNeeded()
        startDjLinkIfEnabled()
        setContent {
            AudiplexTheme {
                RootScaffold()
            }
        }
    }

    /**
     * Start the DJ link from here rather than from Application.onCreate.
     *
     * A foreground-service start is refused when the app is in the background,
     * and Application.onCreate also runs when the process is spun up for a
     * broadcast or a job — precisely the case where the start would be
     * illegal. An activity being created is unambiguously foreground. (The
     * other legal entry point, a reboot, is BootReceiver's job.)
     */
    private fun startDjLinkIfEnabled() {
        lifecycleScope.launch {
            if (settingsStore.djLinkEnabled.first()) {
                DjLinkService.start(this@MainActivity)
            }
        }
    }

    /**
     * The link's notification is its only visible surface and its liveness
     * readout, so ask once. Denial does not stop the service — a foreground
     * service still runs without the permission, the notification is just
     * suppressed — so nothing here blocks on the answer.
     */
    private fun askForNotificationsIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
    }
}
