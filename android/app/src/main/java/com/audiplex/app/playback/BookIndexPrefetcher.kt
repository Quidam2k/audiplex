package com.audiplex.app.playback

import android.content.Context
import android.net.ConnectivityManager
import android.util.Log
import androidx.annotation.OptIn
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.cache.CacheWriter
import androidx.media3.datasource.okhttp.OkHttpDataSource
import com.audiplex.app.data.ApiServiceHolder
import com.audiplex.app.data.api.AudiplexApi
import com.audiplex.app.data.api.BookDetail
import com.audiplex.app.data.download.DownloadRepository
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import java.util.Collections
import javax.inject.Inject
import javax.inject.Singleton

/** #4217: the byte ranges a resume reads before it can play: the file head and the moov index. */
internal fun indexRanges(moovOffset: Long?, moovSize: Long?, fileSize: Long): List<LongRange> {
    if (moovOffset == null || moovSize == null || moovSize <= 0 || fileSize <= 0) return emptyList()
    if (moovOffset < 0 || moovOffset + moovSize > fileSize) return emptyList()
    val head = 0L until minOf(HEAD_BYTES, fileSize)
    val moov = moovOffset until moovOffset + moovSize
    return if (moov.first <= head.last + 1) listOf(0L until maxOf(head.last + 1, moov.last + 1)) else listOf(head, moov)
}

internal const val HEAD_BYTES = 64L * 1024

/** The cache key playback uses for a streamed book (PlaybackManager.buildMediaItem). */
internal fun bookCacheKey(url: String, fileSize: Long) = "$url@$fileSize"

/** True when the book's head + moov are already on disk (for the time-to-ready log). */
@OptIn(UnstableApi::class)
internal fun bookIndexCached(context: Context, book: BookDetail, url: String): Boolean {
    val ranges = indexRanges(book.moovOffset, book.moovSize, book.fileSize)
    if (ranges.isEmpty()) return false
    val cache = StreamCache.books(context)
    val key = bookCacheKey(url, book.fileSize)
    return ranges.all { cache.isCached(key, it.first, it.last - it.first + 1) }
}

/**
 * #4217: warms the book cache with each in-progress book's head + moov index, so a
 * resume reads them from disk instead of fetching ~5 MB first. Runs only on an
 * unmetered network and never while anything is playing, so it can't cost cellular
 * data or stutter a ride.
 */
@OptIn(UnstableApi::class)
@Singleton
class BookIndexPrefetcher @Inject constructor(
    @ApplicationContext private val context: Context,
    private val apiHolder: ApiServiceHolder,
    private val downloadRepository: DownloadRepository,
    private val okHttpClient: OkHttpClient,
    private val playbackManager: PlaybackManager,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val warmed = Collections.synchronizedSet(mutableSetOf<String>())
    @Volatile private var job: Job? = null

    /** Warm up to [MAX_BOOKS] books, most recent first. Skipped while a warm-up is still running. */
    @Synchronized
    fun warm(bookIds: List<Int>) {
        if (bookIds.isEmpty() || job?.isActive == true) return
        job = scope.launch {
            for (id in bookIds.take(MAX_BOOKS)) {
                if (!allowed()) return@launch
                runCatching { warmOne(id) }.onFailure { Log.w(TAG, "book $id: $it") }
            }
        }
    }

    private fun allowed(): Boolean {
        if (playbackManager.isPlaying.value) return false
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return false
        return cm.activeNetwork != null && !cm.isActiveNetworkMetered
    }

    private suspend fun warmOne(id: Int) {
        if (downloadRepository.getLocalPath(id) != null) return
        val api = apiHolder.api ?: return
        val book: BookDetail = api.getBook(id)
        if (book.category == "audiobook_misc") return
        val url = AudiplexApi.streamUrl(apiHolder.baseUrl, book.id)
        val key = bookCacheKey(url, book.fileSize)
        if (key in warmed) return
        val ranges = indexRanges(book.moovOffset, book.moovSize, book.fileSize)
        if (ranges.isEmpty()) return
        val source = StreamCache.bookFactory(context, OkHttpDataSource.Factory(okHttpClient)).createDataSource()
        val started = System.currentTimeMillis()
        for (r in ranges) {
            if (!allowed()) return
            val spec = DataSpec.Builder().setUri(url).setPosition(r.first)
                .setLength(r.last - r.first + 1).setKey(key).build()
            CacheWriter(source, spec, null, null).cache()
        }
        warmed += key
        Log.i(TAG, "book $id index warm in ${System.currentTimeMillis() - started} ms")
    }

    companion object {
        private const val TAG = "BookIndexPrefetcher"
        const val MAX_BOOKS = 3
    }
}
