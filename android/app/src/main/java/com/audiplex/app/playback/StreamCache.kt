package com.audiplex.app.playback

import android.content.Context
import androidx.annotation.OptIn
import androidx.media3.common.util.UnstableApi
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import java.io.File

/**
 * #4217: resuming a book took ~5 s. Every one of the 610 books (Libation output) keeps
 * its moov index at the END of the file: median 5 MB, Anathem 11.4 MB, up to 22 MB.
 * With no cache, ExoPlayer re-downloaded that index on every resume before it could
 * seek. Streamed bytes now land in an on-disk LRU cache, so a book's index is fetched
 * once and later resumes read it from disk. Each resume re-reads the index, which
 * keeps it fresh in the LRU. Downloaded books still play from their local file.
 */
@OptIn(UnstableApi::class)
object StreamCache {
    private const val MAX_BYTES = 512L * 1024 * 1024
    @Volatile private var cache: SimpleCache? = null

    /** One SimpleCache per process: Media3 refuses a second instance on the same folder. */
    fun get(context: Context): SimpleCache = cache ?: synchronized(this) {
        cache ?: SimpleCache(
            File(context.cacheDir, "stream_cache"),
            LeastRecentlyUsedCacheEvictor(MAX_BYTES),
            StandaloneDatabaseProvider(context.applicationContext),
        ).also { cache = it }
    }

    /** Wraps the authed upstream; a cache error falls through to the network. */
    fun factory(context: Context, upstream: DataSource.Factory): DataSource.Factory =
        CacheDataSource.Factory()
            .setCache(get(context))
            .setUpstreamDataSourceFactory(upstream)
            .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)
}
