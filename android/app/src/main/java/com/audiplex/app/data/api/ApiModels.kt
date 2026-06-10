package com.audiplex.app.data.api

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

// ----- Auth -----

@JsonClass(generateAdapter = true)
data class LoginRequest(
    val username: String,
    val password: String
)

@JsonClass(generateAdapter = true)
data class RegisterRequest(
    val username: String,
    val password: String,
    @Json(name = "display_name") val displayName: String? = null
)

@JsonClass(generateAdapter = true)
data class UserInfo(
    val id: Int,
    val username: String,
    @Json(name = "display_name") val displayName: String?,
    @Json(name = "is_admin") val isAdmin: Boolean
)

@JsonClass(generateAdapter = true)
data class LoginResponse(
    val token: String,
    val user: UserInfo
)

// ----- Library -----

@JsonClass(generateAdapter = true)
data class BookSummary(
    val id: Int,
    val title: String,
    val author: String?,
    val narrator: String?,
    val series: String?,
    @Json(name = "series_sequence") val seriesSequence: String?,
    @Json(name = "duration_seconds") val durationSeconds: Double,
    @Json(name = "has_cover") val hasCover: Boolean,
    @Json(name = "added_at") val addedAt: String,
    val category: String? = null
)

@JsonClass(generateAdapter = true)
data class ChapterSchema(
    val index: Int,
    val title: String,
    @Json(name = "start_seconds") val startSeconds: Double,
    @Json(name = "end_seconds") val endSeconds: Double?
)

@JsonClass(generateAdapter = true)
data class BookDetail(
    val id: Int,
    val title: String,
    val author: String?,
    val narrator: String?,
    val series: String?,
    @Json(name = "series_sequence") val seriesSequence: String?,
    @Json(name = "duration_seconds") val durationSeconds: Double,
    @Json(name = "has_cover") val hasCover: Boolean,
    @Json(name = "added_at") val addedAt: String,
    @Json(name = "file_size") val fileSize: Long,
    val chapters: List<ChapterSchema> = emptyList(),
    val category: String? = null,
    @Json(name = "track_urls") val trackUrls: List<String> = emptyList()
)

@JsonClass(generateAdapter = true)
data class ProgressSchema(
    @Json(name = "book_id") val bookId: Int,
    @Json(name = "position_seconds") val positionSeconds: Double,
    @Json(name = "chapter_index") val chapterIndex: Int,
    @Json(name = "updated_at") val updatedAt: String,
    @Json(name = "is_finished") val isFinished: Boolean
)

@JsonClass(generateAdapter = true)
data class ProgressUpdate(
    @Json(name = "position_seconds") val positionSeconds: Double,
    @Json(name = "chapter_index") val chapterIndex: Int = 0,
    @Json(name = "is_finished") val isFinished: Boolean = false
)

@JsonClass(generateAdapter = true)
data class AuthorSchema(
    val name: String,
    @Json(name = "book_count") val bookCount: Int
)

@JsonClass(generateAdapter = true)
data class SeriesSchema(
    val name: String,
    @Json(name = "book_count") val bookCount: Int
)

@JsonClass(generateAdapter = true)
data class ScanResult(
    val added: Int,
    val updated: Int,
    val removed: Int,
    val errors: List<String>
)

@JsonClass(generateAdapter = true)
data class HealthResponse(
    val status: String
)

@JsonClass(generateAdapter = true)
data class AppVersionResponse(
    @Json(name = "versionCode") val versionCode: Int,
    @Json(name = "versionName") val versionName: String,
    @Json(name = "sizeBytes") val sizeBytes: Long,
    @Json(name = "mtime") val mtime: Long
)

// ----- Music -----

@JsonClass(generateAdapter = true)
data class MusicRoot(
    val path: String,
    val exists: Boolean
)

@JsonClass(generateAdapter = true)
data class MusicRootsResponse(
    val roots: List<MusicRoot> = emptyList()
)

@JsonClass(generateAdapter = true)
data class MusicRootsUpdate(
    val paths: List<String> = emptyList()
)

@JsonClass(generateAdapter = true)
data class GenreSchema(
    val name: String,
    @Json(name = "album_count") val albumCount: Int
)

@JsonClass(generateAdapter = true)
data class MusicArtistSchema(
    val id: Int,
    val name: String
)

@JsonClass(generateAdapter = true)
data class AlbumSummary(
    val id: Int,
    val title: String,
    @Json(name = "artist_id") val artistId: Int,
    @Json(name = "artist_name") val artistName: String?,
    val genre: String?,
    val year: Int?,
    @Json(name = "duration_seconds") val durationSeconds: Double,
    @Json(name = "track_count") val trackCount: Int,
    @Json(name = "has_cover") val hasCover: Boolean
)

@JsonClass(generateAdapter = true)
data class MusicArtistDetail(
    val id: Int,
    val name: String,
    val albums: List<AlbumSummary> = emptyList()
)

@JsonClass(generateAdapter = true)
data class TrackSchema(
    val id: Int,
    val title: String,
    @Json(name = "album_id") val albumId: Int,
    @Json(name = "artist_id") val artistId: Int,
    @Json(name = "artist_name") val artistName: String?,
    @Json(name = "disc_number") val discNumber: Int,
    @Json(name = "track_number") val trackNumber: Int,
    @Json(name = "duration_seconds") val durationSeconds: Double
)

@JsonClass(generateAdapter = true)
data class AlbumDetail(
    val id: Int,
    val title: String,
    @Json(name = "artist_id") val artistId: Int,
    @Json(name = "artist_name") val artistName: String?,
    val genre: String?,
    val year: Int?,
    @Json(name = "duration_seconds") val durationSeconds: Double,
    @Json(name = "track_count") val trackCount: Int,
    @Json(name = "has_cover") val hasCover: Boolean,
    val tracks: List<TrackSchema> = emptyList()
)

@JsonClass(generateAdapter = true)
data class PlaylistSummary(
    val id: Int,
    val name: String,
    @Json(name = "track_count") val trackCount: Int = 0
)

@JsonClass(generateAdapter = true)
data class PlaylistDetail(
    val id: Int,
    val name: String,
    @Json(name = "track_count") val trackCount: Int = 0,
    val tracks: List<TrackSchema> = emptyList()
)

@JsonClass(generateAdapter = true)
data class PlayStatEvent(
    @Json(name = "track_id") val trackId: Int,
    val event: String,
    @Json(name = "played_seconds") val playedSeconds: Double = 0.0
)

@JsonClass(generateAdapter = true)
data class PlayStatSchema(
    val id: Int,
    @Json(name = "track_id") val trackId: Int,
    val event: String,
    @Json(name = "played_seconds") val playedSeconds: Double,
    val timestamp: String
)

@JsonClass(generateAdapter = true)
data class FavoriteCreate(
    @Json(name = "entity_type") val entityType: String,
    @Json(name = "entity_key") val entityKey: String
)

@JsonClass(generateAdapter = true)
data class FavoriteSchema(
    val id: Int,
    @Json(name = "entity_type") val entityType: String,
    @Json(name = "entity_key") val entityKey: String,
    @Json(name = "created_at") val createdAt: String
)

@JsonClass(generateAdapter = true)
data class PlaylistCreate(
    val name: String,
    @Json(name = "track_ids") val trackIds: List<Int> = emptyList()
)

@JsonClass(generateAdapter = true)
data class PlaylistAppend(
    @Json(name = "track_ids") val trackIds: List<Int>
)

@JsonClass(generateAdapter = true)
data class SkipSuspectSchema(
    val track: TrackSchema,
    @Json(name = "early_skip_count") val earlySkipCount: Int,
    @Json(name = "total_starts") val totalStarts: Int
)

@JsonClass(generateAdapter = true)
data class FolderNode(
    val name: String,
    val path: String,
    @Json(name = "album_count") val albumCount: Int,
    @Json(name = "track_count") val trackCount: Int
)

@JsonClass(generateAdapter = true)
data class FolderListing(
    val path: String? = null,
    val parent: String? = null,
    val folders: List<FolderNode> = emptyList(),
    val albums: List<AlbumSummary> = emptyList()
)
