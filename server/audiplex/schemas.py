from datetime import datetime

from pydantic import BaseModel, Field


class ChapterSchema(BaseModel):
    index: int
    title: str
    start_seconds: float
    end_seconds: float | None = None

    model_config = {"from_attributes": True}


class BookSummary(BaseModel):
    id: int
    title: str
    author: str | None = None
    narrator: str | None = None
    series: str | None = None
    series_sequence: str | None = None
    category: str = "audiobook_clean"
    duration_seconds: float
    has_cover: bool
    added_at: datetime

    model_config = {"from_attributes": True}


class BookDetail(BookSummary):
    file_size: int
    chapters: list[ChapterSchema] = []
    track_urls: list[str] = []


class ProgressSchema(BaseModel):
    book_id: int
    position_seconds: float
    chapter_index: int
    updated_at: datetime
    is_finished: bool

    model_config = {"from_attributes": True}


class ProgressUpdate(BaseModel):
    position_seconds: float
    chapter_index: int = 0
    is_finished: bool = False


class ScanResultSchema(BaseModel):
    added: int
    updated: int
    removed: int
    errors: list[str]


class AuthorSchema(BaseModel):
    name: str
    book_count: int


class SeriesSchema(BaseModel):
    name: str
    book_count: int


class ArtistSchema(BaseModel):
    id: int
    name: str
    model_config = {"from_attributes": True}


class GenreSchema(BaseModel):
    name: str
    album_count: int


class TrackSchema(BaseModel):
    id: int
    title: str
    album_id: int
    artist_id: int
    artist_name: str | None = None
    disc_number: int
    track_number: int
    duration_seconds: float
    model_config = {"from_attributes": True}


class AlbumSummary(BaseModel):
    id: int
    title: str
    artist_id: int
    artist_name: str | None = None
    genre: str | None = None
    year: int | None = None
    duration_seconds: float
    track_count: int
    has_cover: bool
    model_config = {"from_attributes": True}


class AlbumDetail(AlbumSummary):
    tracks: list[TrackSchema] = []


class ArtistDetail(ArtistSchema):
    albums: list[AlbumSummary] = []


class FolderNode(BaseModel):
    """A browsable folder in the music tree (derived from album paths)."""

    name: str
    path: str
    album_count: int
    track_count: int


class FolderListing(BaseModel):
    """Contents of one folder: child folders + albums living directly in it.

    `path` is None for the top-level listing (the music roots). `parent`
    is None when the folder is itself a music root.
    """

    path: str | None = None
    parent: str | None = None
    folders: list[FolderNode] = []
    albums: list["AlbumSummary"] = []


class MusicRoot(BaseModel):
    """A configured music library folder, with whether it currently exists on disk."""

    path: str
    exists: bool


class MusicRootsResponse(BaseModel):
    roots: list[MusicRoot] = []


class MusicRootsUpdate(BaseModel):
    paths: list[str] = []


class PlaylistSummary(BaseModel):
    id: int
    name: str
    track_count: int = 0
    model_config = {"from_attributes": True}


class PlaylistDetail(PlaylistSummary):
    tracks: list[TrackSchema] = []


class PlaylistCreate(BaseModel):
    name: str
    track_ids: list[int] = []


class PlaylistUpdate(BaseModel):
    name: str | None = None
    track_ids: list[int] | None = None  # None = leave unchanged; [] = empty the playlist


class PlayStatEvent(BaseModel):
    track_id: int
    event: str
    played_seconds: float = 0.0


class PlayStatSchema(BaseModel):
    id: int
    track_id: int
    event: str
    played_seconds: float
    timestamp: datetime
    model_config = {"from_attributes": True}


class FavoriteCreate(BaseModel):
    entity_type: str
    entity_key: str


class FavoriteSchema(BaseModel):
    id: int
    entity_type: str
    entity_key: str
    created_at: datetime
    model_config = {"from_attributes": True}


class TrackRatingSchema(BaseModel):
    id: int
    track_id: int
    rating: int
    note: str
    updated_at: datetime
    model_config = {"from_attributes": True}


class TrackRatingCreate(BaseModel):
    # 1-5 stars, validated at the edge so a bad client cannot poison the
    # signal the DJ reads (#3024).
    rating: int = Field(ge=1, le=5)
    note: str = ""


class PlaylistAppend(BaseModel):
    track_ids: list[int]


class SkipSuspectSchema(BaseModel):
    """Track that's been skipped early often enough to look suspicious."""

    track: TrackSchema
    early_skip_count: int
    total_starts: int


# ----- DJ playback command bus (remote control) -----


class PlaybackCommand(BaseModel):
    """A command from the DJ agent for the client to execute.

    v1 type: 'play_now' with payload {"track_ids": [int, ...]}.
    """

    type: str
    payload: dict = {}


class PlaybackCommandQueued(BaseModel):
    """Ack returned to the agent after enqueueing a command."""

    id: int
    type: str
    pending: int


class PlaybackCommandAck(BaseModel):
    """The device's verdict on a command it was handed (#900 Phase 3a).

    `status` is 'ok' when the command was carried out; anything else is a
    failure the device is owning up to ('no_tracks', 'error', ...). A failure
    reported is worth far more than the silence that preceded this endpoint —
    on 2026-08-14 a command was consumed and dropped with no trace anywhere.
    """

    status: str = "ok"
    detail: str = ""


class PlaybackCommandAckResult(BaseModel):
    """The registry's record of a command after an ack."""

    id: int
    type: str
    status: str
    created_at: float
    delivered_at: float | None = None
    delivery_count: int = 0
    acked_at: float | None = None
    ack_status: str | None = None
    ack_detail: str = ""


class NowPlayingTrack(BaseModel):
    id: int
    title: str | None = None
    artist: str | None = None


class NowPlayingQueueItem(BaseModel):
    """One entry in the client's current queue, so the agent can DJ with
    full visibility (and issue index-based reorders that mean something)."""

    index: int
    id: int
    title: str | None = None
    artist: str | None = None


class DjClipCreated(BaseModel):
    """Ack for an uploaded DJ voice-break clip (item #431).

    clip_id is the clip's epoch-ms filename stem; url is the path the device
    fetches it from (relative, so the client resolves it against its own
    configured base URL rather than whatever host the agent happened to use).
    """

    clip_id: int
    url: str
    duration_seconds: float | None = None


class PlaybackState(BaseModel):
    """Now-playing snapshot written by the client, read by the agent."""

    playing: bool = False
    track: NowPlayingTrack | None = None
    position_ms: int = 0
    duration_ms: int = 0
    queue_length: int = 0
    queue_index: int = 0
    queue: list[NowPlayingQueueItem] = []
    volume: float | None = None


class ClientLogEntry(BaseModel):
    """A diagnostic shipped up by the Android client (#2961).

    The phone is not reachable over adb from the server host, so player errors
    and process-exit reasons have to travel this way or they are lost. `at` is
    the client's own clock (epoch seconds) and may disagree with the server's —
    the server stamps its own `received_at` on arrival.
    """

    level: str = "info"
    event: str
    message: str = ""
    detail: dict = {}
    at: float | None = None
