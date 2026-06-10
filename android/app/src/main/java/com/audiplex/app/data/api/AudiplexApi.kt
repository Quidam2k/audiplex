package com.audiplex.app.data.api

import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.PUT
import retrofit2.http.Path
import retrofit2.http.Query
import retrofit2.http.Streaming

interface AudiplexApi {

    // ----- Auth -----

    @POST("api/auth/login")
    suspend fun login(@Body body: LoginRequest): LoginResponse

    @POST("api/auth/register")
    suspend fun register(@Body body: RegisterRequest): LoginResponse

    @GET("api/auth/me")
    suspend fun me(): UserInfo

    // ----- General -----

    @GET("api/health")
    suspend fun health(): HealthResponse

    @GET("api/app/version")
    suspend fun getAppVersion(): AppVersionResponse

    @Streaming
    @GET("api/app/download")
    suspend fun downloadApk(): Response<ResponseBody>

    @GET("api/library/books")
    suspend fun getBooks(
        @Query("author") author: String? = null,
        @Query("series") series: String? = null,
        @Query("category") category: String? = null
    ): List<BookSummary>

    @GET("api/library/books/{id}")
    suspend fun getBook(@Path("id") id: Int): BookDetail

    @GET("api/library/authors")
    suspend fun getAuthors(@Query("category") category: String? = null): List<AuthorSchema>

    @GET("api/library/series")
    suspend fun getSeries(@Query("category") category: String? = null): List<SeriesSchema>

    @POST("api/library/scan")
    suspend fun scanLibrary(): ScanResult

    @GET("api/progress")
    suspend fun getAllProgress(): List<ProgressSchema>

    @GET("api/progress/{bookId}")
    suspend fun getProgress(@Path("bookId") bookId: Int): ProgressSchema

    @PUT("api/progress/{bookId}")
    suspend fun updateProgress(
        @Path("bookId") bookId: Int,
        @Body update: ProgressUpdate
    ): ProgressSchema

    // ----- Music -----

    @GET("api/music/genres")
    suspend fun getGenres(): List<GenreSchema>

    @GET("api/music/artists")
    suspend fun getMusicArtists(): List<MusicArtistSchema>

    @GET("api/music/artists/{id}")
    suspend fun getArtist(@Path("id") id: Int): MusicArtistDetail

    @GET("api/music/artists/{id}/tracks")
    suspend fun getArtistTracks(@Path("id") id: Int): List<TrackSchema>

    @GET("api/music/albums")
    suspend fun getAlbums(
        @Query("genre") genre: String? = null,
        @Query("artist_id") artistId: Int? = null
    ): List<AlbumSummary>

    @GET("api/music/albums/{id}")
    suspend fun getAlbum(@Path("id") id: Int): AlbumDetail

    @GET("api/music/tracks/{id}")
    suspend fun getTrack(@Path("id") id: Int): TrackSchema

    @GET("api/music/genres/{name}/tracks")
    suspend fun getGenreTracks(@Path("name") name: String): List<TrackSchema>

    @GET("api/music/roots")
    suspend fun getMusicRoots(): MusicRootsResponse

    @PUT("api/music/roots")
    suspend fun setMusicRoots(@Body body: MusicRootsUpdate): MusicRootsResponse

    @GET("api/music/folders")
    suspend fun getFolders(@Query("path") path: String? = null): FolderListing

    @GET("api/music/folders/tracks")
    suspend fun getFolderTracks(@Query("path") path: String): List<TrackSchema>

    @GET("api/music/playlists")
    suspend fun getPlaylists(): List<PlaylistSummary>

    @GET("api/music/playlists/{id}")
    suspend fun getPlaylist(@Path("id") id: Int): PlaylistDetail

    @POST("api/music/playlists")
    suspend fun createPlaylist(@Body body: PlaylistCreate): PlaylistDetail

    @POST("api/music/playlists/{id}/tracks")
    suspend fun appendToPlaylist(
        @Path("id") id: Int,
        @Body body: PlaylistAppend
    ): PlaylistDetail

    @POST("api/music/stats")
    suspend fun postPlayStat(@Body event: PlayStatEvent): PlayStatSchema

    @GET("api/music/favorites")
    suspend fun getFavorites(
        @Query("entity_type") entityType: String? = null
    ): List<FavoriteSchema>

    @POST("api/music/favorites")
    suspend fun addFavorite(@Body body: FavoriteCreate): FavoriteSchema

    @DELETE("api/music/favorites/{type}/{key}")
    suspend fun deleteFavorite(
        @Path("type") type: String,
        @Path("key") key: String
    ): Map<String, Int>

    @GET("api/music/most-played")
    suspend fun getMostPlayed(@Query("limit") limit: Int = 50): List<TrackSchema>

    @GET("api/music/likely-skips")
    suspend fun getLikelySkips(
        @Query("limit") limit: Int = 25,
        @Query("min_skips") minSkips: Int = 2
    ): List<SkipSuspectSchema>

    companion object {
        fun coverUrl(baseUrl: String, bookId: Int): String =
            "${baseUrl}api/library/books/$bookId/cover"

        fun streamUrl(baseUrl: String, bookId: Int): String =
            "${baseUrl}api/stream/$bookId"

        fun streamTrackUrl(baseUrl: String, bookId: Int, trackIndex: Int): String =
            "${baseUrl}api/stream/$bookId/track/$trackIndex"

        fun musicCoverUrl(baseUrl: String, albumId: Int): String =
            "${baseUrl}api/music/covers/album/$albumId"

        fun musicStreamUrl(baseUrl: String, trackId: Int): String =
            "${baseUrl}api/music/stream/track/$trackId"
    }
}
