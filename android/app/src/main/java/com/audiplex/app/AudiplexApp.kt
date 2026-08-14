package com.audiplex.app

import android.app.Application
import androidx.hilt.work.HiltWorkerFactory
import androidx.work.Configuration
import coil3.ImageLoader
import coil3.SingletonImageLoader
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import com.audiplex.app.playback.ClientLogReporter
import com.audiplex.app.playback.DjCommandClient
import dagger.hilt.android.HiltAndroidApp
import okhttp3.OkHttpClient
import javax.inject.Inject

@HiltAndroidApp
class AudiplexApp : Application(), Configuration.Provider, SingletonImageLoader.Factory {

    @Inject lateinit var workerFactory: HiltWorkerFactory
    @Inject lateinit var okHttpClient: OkHttpClient
    @Inject lateinit var djCommandClient: DjCommandClient
    @Inject lateinit var clientLogReporter: ClientLogReporter

    override fun onCreate() {
        super.onCreate()
        // Start the DJ remote-control bridge for the life of the process. Both
        // loops idle cheaply (long-poll / 5s tick) until a server + token exist.
        djCommandClient.start()
        // Report how the previous process died before doing anything else that
        // might itself die. This is the only account of a silent death that
        // reaches the server (#2961) — nothing else survives the process.
        clientLogReporter.reportProcessExits()
    }

    override val workManagerConfiguration: Configuration
        get() = Configuration.Builder()
            .setWorkerFactory(workerFactory)
            .build()

    override fun newImageLoader(context: coil3.PlatformContext): ImageLoader {
        return ImageLoader.Builder(context)
            .components {
                add(OkHttpNetworkFetcherFactory(callFactory = { okHttpClient }))
            }
            .build()
    }
}
