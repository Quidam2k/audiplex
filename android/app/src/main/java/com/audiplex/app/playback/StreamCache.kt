package com.audiplex.app.playback

import android.content.Context
import android.os.StatFs
import androidx.annotation.OptIn
import androidx.media3.common.util.UnstableApi
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.TransferListener
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import java.io.File

/**
 * #4217: resuming a book took ~5 s. Every one of the 610 books (Libation output) keeps
 * its moov index at the END of the file: median 5 MB, Anathem 11.4 MB, up to 22 MB.
 * With no cache, ExoPlayer re-downloaded that index on every resume before it could
 * seek. Streamed bytes now land in an on-disk LRU cache, so a book's index is fetched
 * once and later resumes read it from disk. Downloaded books still play from their local file.
 *
 * Books and music get SEPARATE caches: one shared LRU let a single music ride
 * (~60 songs x ~8 MB) push every book index out. [BookIndexPrefetcher] warms the
 * book cache ahead of a resume.
 */
@OptIn(UnstableApi::class)
object StreamCache {
    private const val MUSIC_BYTES = 512L * 1024 * 1024
    private const val BOOK_BYTES = 1024L * 1024 * 1024
    private const val BOOK_BYTES_TIGHT = 512L * 1024 * 1024
    /** Below this much free space the book cache drops to [BOOK_BYTES_TIGHT]. */
    private const val TIGHT_FREE_BYTES = 8L * 1024 * 1024 * 1024

    @Volatile private var music: SimpleCache? = null
    @Volatile private var books: SimpleCache? = null
    @Volatile private var dbProvider: StandaloneDatabaseProvider? = null

    /** One SimpleCache per folder per process: Media3 refuses a second instance on the same folder. */
    fun get(context: Context): SimpleCache = music ?: synchronized(this) {
        music ?: SimpleCache(
            File(context.cacheDir, "stream_cache"),
            LeastRecentlyUsedCacheEvictor(MUSIC_BYTES),
            provider(context),
        ).also { music = it }
    }

    fun books(context: Context): SimpleCache = books ?: synchronized(this) {
        books ?: SimpleCache(
            File(context.cacheDir, "book_cache"),
            LeastRecentlyUsedCacheEvictor(bookCapBytes(StatFs(context.cacheDir.path).availableBytes)),
            provider(context),
        ).also { books = it }
    }

    internal fun bookCapBytes(freeBytes: Long): Long =
        if (freeBytes < TIGHT_FREE_BYTES) BOOK_BYTES_TIGHT else BOOK_BYTES

    /** Book streams (/api/stream/...) use the book cache; everything else uses the music cache. */
    internal fun isBookPath(path: String?): Boolean = path?.startsWith("/api/stream/") == true

    private fun provider(context: Context): StandaloneDatabaseProvider =
        dbProvider ?: StandaloneDatabaseProvider(context.applicationContext).also { dbProvider = it }

    private fun cached(cache: SimpleCache, upstream: DataSource.Factory): CacheDataSource.Factory =
        CacheDataSource.Factory()
            .setCache(cache)
            .setUpstreamDataSourceFactory(upstream)
            .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)

    /** The book-cache source on its own, for the prefetcher. */
    fun bookFactory(context: Context, upstream: DataSource.Factory): CacheDataSource.Factory =
        cached(books(context), upstream)

    /** Wraps the authed upstream; a cache error falls through to the network. */
    fun factory(context: Context, upstream: DataSource.Factory): DataSource.Factory {
        val bookF = bookFactory(context, upstream)
        val musicF = cached(get(context), upstream)
        return DataSource.Factory { RoutingDataSource(bookF.createDataSource(), musicF.createDataSource()) }
    }

    /** Picks the book or music cache per open() by URL path. */
    private class RoutingDataSource(
        private val book: DataSource,
        private val music: DataSource,
    ) : DataSource {
        private var active: DataSource? = null

        override fun addTransferListener(transferListener: TransferListener) {
            book.addTransferListener(transferListener)
            music.addTransferListener(transferListener)
        }

        override fun open(dataSpec: DataSpec): Long {
            val target = if (isBookPath(dataSpec.uri.path)) book else music
            active = target
            return target.open(dataSpec)
        }

        override fun read(buffer: ByteArray, offset: Int, length: Int): Int =
            checkNotNull(active).read(buffer, offset, length)

        override fun getUri() = active?.uri

        override fun getResponseHeaders(): Map<String, List<String>> =
            active?.responseHeaders ?: emptyMap()

        override fun close() {
            try { active?.close() } finally { active = null }
        }
    }
}
