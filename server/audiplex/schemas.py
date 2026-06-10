from datetime import datetime

from pydantic import BaseModel


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


class PlaylistAppend(BaseModel):
    track_ids: list[int]


class SkipSuspectSchema(BaseModel):
    """Track that's been skipped early often enough to look suspicious."""

    track: TrackSchema
    early_skip_count: int
    total_starts: int
