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
data class TrackRatingCreate(
    val rating: Int,
    val note: String = "",
)

@JsonClass(generateAdapter = true)
data class TrackRatingSchema(
    val id: Int,
    @Json(name = "track_id") val trackId: Int,
    val rating: Int,
    val note: String,
    @Json(name = "updated_at") val updatedAt: String,
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

// ----- DJ playback command bus (remote control) -----

@JsonClass(generateAdapter = true)
data class DjCommandPayload(
    // 'play_now'/'queue'/'play_next' carry track_ids; 'reorder' carries indices.
    @Json(name = "track_ids") val trackIds: List<Int>? = null,
    @Json(name = "from_index") val fromIndex: Int? = null,
    @Json(name = "to_index") val toIndex: Int? = null,
    // 'seek' carries position_ms; 'volume' carries volume (0.0-1.0).
    @Json(name = "position_ms") val positionMs: Long? = null,
    val volume: Float? = null,
    // 'play_stream' carries an external HTTP audio stream URL + display title.
    val url: String? = null,
    val title: String? = null,
    // 'announce' carries a synthesized DJ voice-break clip (item #431).
    // clipId is an epoch-ms id, so it must be a Long — it does NOT fit in Int.
    @Json(name = "clip_id") val clipId: Long? = null,
    @Json(name = "clip_url") val clipUrl: String? = null,
    @Json(name = "duration_seconds") val durationSeconds: Double? = null,
    // 'next' (play after the current track) or 'now' (interrupt).
    val mode: String? = null
)

@JsonClass(generateAdapter = true)
data class DjCommandDto(
    val id: Long,
    val type: String,
    val payload: DjCommandPayload? = null,
    @Json(name = "created_at") val createdAt: Double? = null,
    // >1 means the server never heard our ack for this command and is offering
    // it again. Delivery is at-least-once, so the client dedupes on id (#900).
    @Json(name = "delivery_count") val deliveryCount: Int = 1
)

/**
 * What we did with a command (#900 Phase 3a).
 *
 * `status` is "ok" when it was carried out, otherwise a short machine-readable
 * reason ("no_tracks", "bad_payload", "unknown_type", "error"). Reporting a
 * FAILURE matters as much as reporting success: on 2026-08-14 a command was
 * taken off the queue and silently dropped, and nothing anywhere could tell
 * that apart from one still in flight.
 */
@JsonClass(generateAdapter = true)
data class DjCommandAckDto(
    val status: String,
    val detail: String = ""
)

@JsonClass(generateAdapter = true)
data class NowPlayingTrackDto(
    val id: Int,
    val title: String?,
    val artist: String?
)

@JsonClass(generateAdapter = true)
data class QueueTrackDto(
    val index: Int,
    val id: Int,
    val title: String?,
    val artist: String?
)

/**
 * A diagnostic shipped up to the server (#2961). The phone isn't reachable over
 * adb from the server host, so a playback error or a process death is invisible
 * unless the app reports it itself.
 */
@JsonClass(generateAdapter = true)
data class ClientLogDto(
    val level: String,
    val event: String,
    val message: String = "",
    val detail: Map<String, String> = emptyMap(),
    val at: Double? = null
)

@JsonClass(generateAdapter = true)
data class PlaybackStateDto(
    val playing: Boolean,
    val track: NowPlayingTrackDto?,
    @Json(name = "position_ms") val positionMs: Long,
    @Json(name = "duration_ms") val durationMs: Long,
    @Json(name = "queue_length") val queueLength: Int,
    @Json(name = "queue_index") val queueIndex: Int,
    val queue: List<QueueTrackDto> = emptyList(),
    val volume: Float? = null
)
