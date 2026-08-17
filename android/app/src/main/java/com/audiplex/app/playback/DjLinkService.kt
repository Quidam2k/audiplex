package com.audiplex.app.playback

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.audiplex.app.MainActivity
import com.audiplex.app.R
import com.audiplex.app.data.SettingsStore
import dagger.hilt.android.AndroidEntryPoint
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import javax.inject.Inject

/**
 * Keeps the app process alive so the DJ can reach it (#3022).
 *
 * ## Why this exists
 *
 * The 2026-08-14 failure was not a playback bug in the end — it was that the
 * app was simply not running. A cached Android app is a candidate for
 * reclamation at any moment, and the device's own exit history is a wall of
 * LOW_MEMORY and SIGKILL. A command queued for a dead app goes nowhere, and
 * from the DJ's side that is indistinguishable from a command that was
 * delivered and ignored. Todd's ruling was to make the link always-on and
 * accept the notification and battery cost.
 *
 * ## What it deliberately does NOT do
 *
 * It owns nothing. [DjCommandClient]'s two loops stay exactly where they were,
 * in the application scope, untouched — this service only holds the process up
 * and shows a notification. Switch it off and behaviour is bit-for-bit what it
 * was before it existed, just vulnerable to process death again. That is the
 * reversibility Todd asked for ("we can change our mind later"), and it only
 * holds as long as nothing else is allowed to depend on this service running.
 * Read the link state, never drive it.
 *
 * ## specialUse, not dataSync
 *
 * Android 15 caps `dataSync` foreground services at 6 hours per 24, which
 * would silently kill an always-on link. That cap is gated on targetSdk 35+
 * and we are on 34, so `dataSync` would work *today* and start failing
 * whenever someone bumps targetSdk — a regression that would surface as
 * "the DJ stopped working in the evenings" and point nowhere near the commit
 * that caused it. `specialUse` carries no such timeout at any targetSdk. The
 * Play Store review that normally makes `specialUse` awkward does not apply:
 * this app is sideloaded.
 */
@AndroidEntryPoint
class DjLinkService : Service() {

    @Inject lateinit var djCommandClient: DjCommandClient
    @Inject lateinit var settingsStore: SettingsStore

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    override fun onCreate() {
        super.onCreate()
        createChannel()
        // Must be immediate: Android gives a started service ~5s to show its
        // notification before it kills the process for not doing so.
        startForeground(NOTIFICATION_ID, buildNotification(LinkState.UNCONFIGURED))

        scope.launch {
            djCommandClient.linkState.collectLatest { state ->
                notificationManager().notify(NOTIFICATION_ID, buildNotification(state))
            }
        }
        // Honour the toggle even if it is flipped while we are already up.
        scope.launch {
            settingsStore.djLinkEnabled.collectLatest { enabled ->
                if (!enabled) stopSelf()
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            // The notification's "Turn off" action: persist the choice, don't
            // just stop, or the next launch would silently bring it back.
            scope.launch { settingsStore.setDjLinkEnabled(false) }
            stopSelf()
            return START_NOT_STICKY
        }
        // START_STICKY: if the OS does reclaim us under memory pressure, come
        // back. That is the entire point of the service.
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            "DJ link",
            // MIN: this notification is a permanent fixture, so it must never
            // make a sound, vibrate, or push anything else down the shade. On
            // recent Android it collapses into the "running in background"
            // chip, which is as close to invisible as an FGS is allowed to be.
            NotificationManager.IMPORTANCE_MIN,
        ).apply {
            description = "Keeps the remote-control link to the Audiplex server alive."
            setShowBadge(false)
        }
        notificationManager().createNotificationChannel(channel)
    }

    private fun buildNotification(state: LinkState): Notification {
        // The text is the whole point: a static "service running" would say
        // nothing, whereas this doubles as the at-a-glance liveness readout
        // whose absence is what made the 08-14 test fail silently.
        val text = when (state) {
            LinkState.CONNECTED -> "Connected — ready for DJ commands"
            LinkState.OFFLINE -> "Can't reach the server — retrying"
            LinkState.UNCONFIGURED -> "Waiting for sign-in"
        }
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val turnOff = PendingIntent.getService(
            this,
            1,
            Intent(this, DjLinkService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("DJ link")
            .setContentText(text)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentIntent(open)
            .addAction(0, "Turn off", turnOff)
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()
    }

    private fun notificationManager() =
        getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    companion object {
        private const val CHANNEL_ID = "dj_link"
        private const val NOTIFICATION_ID = 4201
        const val ACTION_STOP = "com.audiplex.app.DJ_LINK_STOP"

        /**
         * Start the link if the user wants it.
         *
         * Wrapped because starting a foreground service is refused outright in
         * some background states. Callers are places where it is permitted —
         * a visible activity, or the boot-completed exemption — but a refusal
         * must never take the app down with it: the link is a reliability
         * feature, and crashing the process to protect it would be absurd.
         */
        fun start(context: Context) {
            runCatching {
                context.startForegroundService(Intent(context, DjLinkService::class.java))
            }
        }

        fun stop(context: Context) {
            runCatching { context.stopService(Intent(context, DjLinkService::class.java)) }
        }
    }
}
