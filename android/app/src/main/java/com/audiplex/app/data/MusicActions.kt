package com.audiplex.app.data

import com.audiplex.app.data.api.PlaylistAppend
import com.audiplex.app.data.api.PlaylistCreate
import com.audiplex.app.data.api.PlaylistDetail
import com.audiplex.app.data.api.PlaylistSummary
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Server-call helpers shared across music screens.
 *
 * Each `appendTo…` method resolves the target entity (artist, album,
 * genre, playlist) to a flat list of track ids, then either appends to
 * an existing playlist or creates a new one with that list.
 */
@Singleton
class MusicActions @Inject constructor(
    private val apiHolder: ApiServiceHolder
) {
    suspend fun loadPlaylists(): List<PlaylistSummary> =
        runCatching { apiHolder.api?.getPlaylists().orEmpty() }.getOrDefault(emptyList())

    suspend fun appendTrackIds(playlistId: Int, trackIds: List<Int>): PlaylistDetail? {
        if (trackIds.isEmpty()) return null
        val api = apiHolder.api ?: return null
        return runCatching {
            api.appendToPlaylist(playlistId, PlaylistAppend(trackIds = trackIds))
        }.getOrNull()
    }

    suspend fun createWithTracks(name: String, trackIds: List<Int>): PlaylistDetail? {
        val api = apiHolder.api ?: return null
        return runCatching {
            api.createPlaylist(PlaylistCreate(name = name, trackIds = trackIds))
        }.getOrNull()
    }

    /** Resolve an entity (kind, key) into the flat track-id list it represents. */
    suspend fun resolveTrackIds(kind: String, key: String): List<Int> {
        val api = apiHolder.api ?: return emptyList()
        return try {
            when (kind) {
                "track" -> listOf(key.toInt())
                "album" -> api.getAlbum(key.toInt()).tracks.map { it.id }
                "artist" -> api.getArtistTracks(key.toInt()).map { it.id }
                "genre" -> api.getGenreTracks(key).map { it.id }
                "folder" -> api.getFolderTracks(key).map { it.id }
                "playlist" -> api.getPlaylist(key.toInt()).tracks.map { it.id }
                else -> emptyList()
            }
        } catch (_: Exception) {
            emptyList()
        }
    }
}
