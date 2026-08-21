package com.audiplex.app.data.db

import android.content.Context
import androidx.room.Room
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

/**
 * v1 → v2: adds the playback_positions table. Only CREATEs the new table —
 * the existing downloads table (and the files it points at on disk) is left
 * untouched. Never use destructive migration here: dropping downloads would
 * orphan the downloaded audio files.
 */
val MIGRATION_1_2 = object : Migration(1, 2) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS playback_positions (
                book_id INTEGER NOT NULL PRIMARY KEY,
                position_seconds REAL NOT NULL,
                chapter_index INTEGER NOT NULL,
                is_finished INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                synced_to_server INTEGER NOT NULL
            )
            """.trimIndent()
        )
    }
}

/**
 * v2 → v3: adds the recently_played_tracks history table (#3107/#993). Additive
 * only — CREATEs the new table and touches nothing else, so downloads and saved
 * playback positions upgrade losslessly. Never destructive here. Column
 * affinities/nullability match RecentlyPlayedTrackEntity exactly (artist_name
 * nullable TEXT, the rest NOT NULL) so Room's open-time schema check passes.
 */
val MIGRATION_2_3 = object : Migration(2, 3) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS recently_played_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                track_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                artist_id INTEGER NOT NULL,
                artist_name TEXT,
                album_id INTEGER NOT NULL,
                album_title TEXT NOT NULL,
                album_has_cover INTEGER NOT NULL,
                disc_number INTEGER NOT NULL,
                track_number INTEGER NOT NULL,
                duration_seconds REAL NOT NULL,
                played_at INTEGER NOT NULL
            )
            """.trimIndent()
        )
    }
}

@Module
@InstallIn(SingletonComponent::class)
object DbModule {

    @Provides
    @Singleton
    fun provideDatabase(@ApplicationContext context: Context): AppDatabase =
        Room.databaseBuilder(context, AppDatabase::class.java, "audiplex.db")
            .addMigrations(MIGRATION_1_2, MIGRATION_2_3)
            .build()

    @Provides
    fun provideDownloadDao(db: AppDatabase): DownloadDao = db.downloadDao()

    @Provides
    fun providePlaybackPositionDao(db: AppDatabase): PlaybackPositionDao = db.playbackPositionDao()

    @Provides
    fun provideRecentlyPlayedDao(db: AppDatabase): RecentlyPlayedDao = db.recentlyPlayedDao()
}
