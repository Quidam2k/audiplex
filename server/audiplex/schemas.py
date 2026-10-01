from datetime import datetime

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


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
    # #2680: when the client sampled this position. A write older than the
    # stored row is refused (409) so a late PC push can't clobber a newer phone
    # position. The phone doesn't send it and keeps last-write-wins.
    client_updated_at: datetime | None = None


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
    # #6117: `rating` stays a WHOLE star (the floor) because app builds before
    # 1.0.51 parse it as Int and would drop the whole list on a 4.5. `stars`
    # is the exact value, halves included.
    rating: int
    stars: float = Field(validation_alias=AliasChoices("stars", "rating"))
    note: str
    updated_at: datetime
    model_config = {"from_attributes": True}

    @field_validator("rating", mode="before")
    @classmethod
    def _whole_star(cls, v):  # #6117
        return int(v)


def _half_steps(v: float) -> float:  # #6117
    if v * 2 != int(v * 2):
        raise ValueError("Stars go in halves: 1, 1.5 ... 5.")
    return v


class TrackRatingCreate(BaseModel):
    # 1-5 stars, validated at the edge so a bad client cannot poison the
    # signal the DJ reads (#3024). #6117: `stars` (halves) from 1.0.51 on;
    # older builds send a whole `rating`.
    rating: int | None = Field(default=None, ge=1, le=5)
    stars: float | None = Field(default=None, ge=0.5, le=5)
    note: str = ""

    @model_validator(mode="after")
    def _one_value(self):  # #6117
        if self.stars is None and self.rating is None:
            raise ValueError("Give stars (or rating).")
        if self.stars is not None:
            _half_steps(self.stars)
        return self

    @property
    def value(self) -> float:  # #6117
        return float(self.stars if self.stars is not None else self.rating)


class VerbalRatingRequest(BaseModel):
    """A star rating Todd said out loud, relayed by a persona (#3576).

    stars may be a half (4.5) — he says those — but the app's star field is a
    whole Int, so the server stores the floor and keeps the exact number in
    the note. Never show him a 5 he didn't say.
    """

    track_ids: list[int] = Field(min_length=1)
    stars: float = Field(ge=0.5, le=5)
    words: str = ""
    persona: str = ""


class PlaylistAppend(BaseModel):
    track_ids: list[int]


class SkipSuspectSchema(BaseModel):
    """Track that's been skipped early often enough to look suspicious."""

    track: TrackSchema
    early_skip_count: int
    total_starts: int


# ----- Song identity, taste aggregation, recency cooldown (#943/#947/#948) -----


class TrackIdentitySchema(BaseModel):
    """The two keys a track answers to. See audiplex/identity.py."""

    track_id: int
    recording_id: str  # same audio — ratings and stats bind here
    work_id: str  # same song, any version — cooldown binds here
    same_recording_track_ids: list[int] = []
    same_work_track_ids: list[int] = []


class RecordingStatsSchema(BaseModel):
    """One recording's listening history, pooled across every copy of it.

    completion_rate is completes/starts, and is null when the recording has
    never been started. Note what 'complete' means: the client posts it with
    the track's full duration, so it records REACHED THE END, not heard all
    of it. Good enough for taste; not evidence of attention.
    """

    recording_id: str
    work_id: str
    track: TrackSchema
    track_ids: list[int]
    starts: int
    completes: int
    abandons: int
    early_skips: int
    completion_rate: float | None = None
    mean_skip_seconds: float | None = None
    median_skip_seconds: float | None = None
    last_played_at: datetime | None = None


class RecentPlaySchema(BaseModel):
    track_id: int
    recording_id: str
    work_id: str
    event: str
    at: datetime
    minutes_ago: float
    title: str | None = None
    artist_name: str | None = None


class CooldownStateSchema(BaseModel):
    recording_cooldown_minutes: float
    work_cooldown_minutes: float
    recent_plays: list[RecentPlaySchema]


class CandidateFilterRequest(BaseModel):
    """Ask whether these picks would repeat something Todd just heard."""

    track_ids: list[int]
    recording_cooldown_minutes: float | None = None
    work_cooldown_minutes: float | None = None
    min_rating: int | None = Field(default=None, ge=1, le=5)


class SuppressionSchema(BaseModel):
    """A candidate to pass over, and the reason to say out loud."""

    track_id: int
    reason: str  # recording_cooldown | work_cooldown | low_rating
    detail: str
    minutes_ago: float | None = None
    clears_in_minutes: float | None = None
    title: str | None = None
    artist_name: str | None = None


class CandidateFilterResult(BaseModel):
    allowed: list[int]
    suppressed: list[SuppressionSchema]
    recording_cooldown_minutes: float
    work_cooldown_minutes: float


class MixPlanRequest(BaseModel):
    """What the phone is doing now, plus the tracks a dj_mix call adds (#2842)."""

    current_id: int | None = None
    played_ids: list[int] = []
    upcoming_ids: list[int] = []
    new_ids: list[int] = []
    shuffle: bool = True
    seed: int | None = None


class MixPlanResult(BaseModel):
    """The new tail of the queue — everything AFTER the current track."""

    upcoming: list[int]
    kept_from_queue: int
    added: int
    trimmed_played: list[int]
    trimmed_duplicates: list[int]
    summary: str
    skipped_long: list[int] = []  # #3249: music over MAX_MIX_TRACK_SECONDS


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
    detail: str | None = ""  # #ride0928: a null detail must not 422 the ack (= silent redelivery)


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


class NowPlayingBook(BaseModel):
    """An audiobook on the PC renderer (#2680); position_ms is book-global."""

    id: int
    title: str | None = None
    chapter_index: int = 0


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
    book: NowPlayingBook | None = None  # #2680
    position_ms: int = 0
    duration_ms: int = 0
    queue_length: int = 0
    queue_index: int = 0
    queue: list[NowPlayingQueueItem] = []
    volume: float | None = None
    # #3505: which app build is reporting, so the DJ can tell "phone too old"
    # before it sends a command the build doesn't know. None from older builds.
    app_version_name: str | None = None
    app_version_code: int | None = None
    # #3601: who built the current queue: "dj" (a playback-bus command) or
    # "manual" (a tap in the app). None from builds that predate it.
    queue_origin: str | None = None


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
