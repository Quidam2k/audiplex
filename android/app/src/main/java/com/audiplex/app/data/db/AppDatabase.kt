package com.audiplex.app.data.db

import androidx.room.Database
import androidx.room.RoomDatabase

@Database(
    entities = [DownloadEntity::class, PlaybackPositionEntity::class, RecentlyPlayedTrackEntity::class],
    version = 3,
    exportSchema = false
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun downloadDao(): DownloadDao
    abstract fun playbackPositionDao(): PlaybackPositionDao
    abstract fun recentlyPlayedDao(): RecentlyPlayedDao
}
