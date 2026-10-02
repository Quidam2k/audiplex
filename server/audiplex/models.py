from datetime import datetime, timezone

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audiplex.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class Book(Base):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    author: Mapped[str | None] = mapped_column(String(500))
    narrator: Mapped[str | None] = mapped_column(String(500))
    series: Mapped[str | None] = mapped_column(String(500))
    series_sequence: Mapped[str | None] = mapped_column(String(50))
    series_raw: Mapped[str | None] = mapped_column(String(500))
    series_source: Mapped[str | None] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(
        String(50), default="audiobook_clean", nullable=False, index=True
    )
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    file_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    cue_path: Mapped[str | None] = mapped_column(Text)
    has_cover: Mapped[bool] = mapped_column(Boolean, default=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    file_hash: Mapped[str | None] = mapped_column(String(64))
    added_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    chapters: Mapped[list["Chapter"]] = relationship(
        back_populates="book", cascade="all, delete-orphan", order_by="Chapter.index"
    )
    progress: Mapped[list["PlaybackPosition"]] = relationship(
        back_populates="book", cascade="all, delete-orphan"
    )


class Chapter(Base):
    __tablename__ = "chapters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"))
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    end_seconds: Mapped[float | None] = mapped_column(Float)
    file_path: Mapped[str | None] = mapped_column(Text)

    book: Mapped["Book"] = relationship(back_populates="chapters")


class PlaybackPosition(Base):
    __tablename__ = "playback_positions"
    __table_args__ = (UniqueConstraint("user_id", "book_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    position_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    chapter_index: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
    is_finished: Mapped[bool] = mapped_column(Boolean, default=False)

    book: Mapped["Book"] = relationship(back_populates="progress")


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    added_at: Mapped[datetime] = mapped_column(default=_utcnow)

    albums: Mapped[list["Album"]] = relationship(
        back_populates="artist", cascade="all, delete-orphan"
    )
    tracks: Mapped[list["Track"]] = relationship(back_populates="artist")


class Album(Base):
    __tablename__ = "albums"
    # Identity is folder_path (unique below). The same (artist, title) can
    # legitimately appear under multiple genre folders in Todd's library
    # (e.g., a Lovage album cross-filed under both "Rock" and
    # "Ambient, Electronic, Trip-Hop").

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    artist_id: Mapped[int] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"), index=True
    )
    genre: Mapped[str | None] = mapped_column(String(200), index=True)
    year: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    track_count: Mapped[int] = mapped_column(Integer, default=0)
    has_cover: Mapped[bool] = mapped_column(Boolean, default=False)
    folder_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    added_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    artist: Mapped["Artist"] = relationship(back_populates="albums")
    tracks: Mapped[list["Track"]] = relationship(
        back_populates="album",
        cascade="all, delete-orphan",
        order_by="(Track.disc_number, Track.track_number)",
    )


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    album_id: Mapped[int] = mapped_column(
        ForeignKey("albums.id", ondelete="CASCADE"), index=True
    )
    artist_id: Mapped[int] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"), index=True
    )
    disc_number: Mapped[int] = mapped_column(Integer, default=1)
    track_number: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    file_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    file_hash: Mapped[str | None] = mapped_column(String(64))
    # #ride0928: music | podcast | clip | ambient. Only 'music' goes into DJ mixes/pools.
    content_kind: Mapped[str] = mapped_column(
        String(20), default="music", server_default="music", nullable=False, index=True
    )
    # #3255: EBU R128 integrated loudness (LUFS), filled by scripts/measure_loudness.py.
    loudness_lufs: Mapped[float | None] = mapped_column(Float, nullable=True)
    # #2806: 0-100 measured energy (loudness + onset density), filled by scripts/measure_energy.py.
    energy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # #1002: tempo + key from scripts/measure_tempo_key.py. musical_key is a Camelot
    # code ('8B' = C major, '8A' = A minor); beat_offset = first beat (s) mod one beat.
    bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    bpm_conf: Mapped[float | None] = mapped_column(Float, nullable=True)
    beat_offset: Mapped[float | None] = mapped_column(Float, nullable=True)
    musical_key: Mapped[str | None] = mapped_column(String(4), nullable=True)
    key_conf: Mapped[float | None] = mapped_column(Float, nullable=True)
    added_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    album: Mapped["Album"] = relationship(back_populates="tracks")
    artist: Mapped["Artist"] = relationship(back_populates="tracks")
    playlist_entries: Mapped[list["PlaylistTrack"]] = relationship(
        back_populates="track", cascade="all, delete-orphan"
    )
    play_stats: Mapped[list["PlayStat"]] = relationship(
        back_populates="track", cascade="all, delete-orphan"
    )


class Playlist(Base):
    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    entries: Mapped[list["PlaylistTrack"]] = relationship(
        back_populates="playlist",
        cascade="all, delete-orphan",
        order_by="PlaylistTrack.position",
    )


class PlaylistTrack(Base):
    __tablename__ = "playlist_tracks"
    __table_args__ = (UniqueConstraint("playlist_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    playlist_id: Mapped[int] = mapped_column(
        ForeignKey("playlists.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[int] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    playlist: Mapped["Playlist"] = relationship(back_populates="entries")
    track: Mapped["Track"] = relationship(back_populates="playlist_entries")


class PlayStat(Base):
    __tablename__ = "play_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[int] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    event: Mapped[str] = mapped_column(String(20), nullable=False)
    played_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    timestamp: Mapped[datetime] = mapped_column(default=_utcnow, index=True)

    track: Mapped["Track"] = relationship(back_populates="play_stats")


class TrackRating(Base):
    """Todd's 1-5 star rating for a track (#3024).

    Its own table rather than a Favorite row: Favorite is binary with a
    polymorphic string key and no room for a score. Tracks only — the DJ
    reasons about what to play next, and a starred album says much less about
    the next three minutes than a starred track does.

    Distinct from the MCP-side `recs` table, which rates RECOMMENDATIONS the
    DJ made about music that may not be in the library at all. These are
    ratings of tracks that exist here.
    """

    __tablename__ = "track_ratings"
    __table_args__ = (UniqueConstraint("user_id", "track_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[int] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    # #6117: stars in 0.5 steps. The column was declared INTEGER; SQLite's
    # affinity keeps 4.5 as REAL there, so no table change was needed.
    rating: Mapped[float] = mapped_column(Float, nullable=False)
    note: Mapped[str] = mapped_column(String(500), default="")
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)


# Favorite uses a polymorphic key so genres (string-keyed) and
# tracks/albums/artists/books (int-keyed) share one table.
class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "entity_type", "entity_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    entity_key: Mapped[str] = mapped_column(String(500), nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class TrackTagRepair(Base):
    """A proposed correction to one file's artist/title, and the case for it.

    An OVERLAY, not an edit. Todd's original files are never rewritten and the
    YouTube channel tag stays where it is, because it is the evidence — once
    overwritten there is no second opinion left to appeal to.

    Keyed by `file_path` rather than `track_id` for a specific reason: the music
    scanner used to delete every track in a changed album and re-insert it, and
    `tracks.id` is a plain INTEGER PRIMARY KEY, so SQLite reuses rowids. A
    repair bound to a track id would follow the id onto a DIFFERENT song. The
    path outlives the row; `source_url` (the YouTube URL from the file's comment
    tag) outlives even a rename, which is why both are kept.

    `status` gates writing, and only `high` confidence is ever set to `applied`
    automatically. A wrong artist is worse than a blank one: identity keys
    derive from normalized(title, artist), so a bad guess quietly poisons
    ratings and cooldown, while a blank one only limits them.
    """

    __tablename__ = "track_tag_repairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_url: Mapped[str | None] = mapped_column(String(500), index=True)
    proposed_artist: Mapped[str | None] = mapped_column(String(500))
    proposed_title: Mapped[str | None] = mapped_column(String(500))
    proposed_album: Mapped[str | None] = mapped_column(String(500))
    confidence: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), default="parser", nullable=False)
    evidence: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        String(20), default="pending_review", nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class DjPairNote(Base):
    """A DJ's note about a track, or about playing track_a into track_b (#ride0928).

    Lives in audiplex.db so what a persona learned on one ride ("this into that
    is a great lift", "never after the ballad") is there on the next. track_b
    NULL = a note about track_a alone.
    """

    __tablename__ = "dj_pair_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_a: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    track_b: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    persona: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class DjBan(Base):
    """A track the DJ must never pick again (#2806). Reversible: delete the row.

    Honored by mix plans and pool picks, across every copy of the same
    recording. An explicit dj_play_now by id still plays: a song asked for by
    name wins.
    """

    __tablename__ = "dj_bans"

    track_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reason: Mapped[str | None] = mapped_column(Text)
    persona: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class DjTrackTag(Base):
    """A mood/vibe tag the DJ put on a track (#2806): "chill", "anthem", "rainy".

    Applied by a DJ persona (or Todd), never inferred. Tags feed dj_energy_set
    filters and the 'tag' mix/pool source. Reversible: delete the row.
    """

    __tablename__ = "dj_track_tags"

    track_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag: Mapped[str] = mapped_column(String(40), primary_key=True, index=True)
    persona: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class MusicVideoJob(Base):
    """One "Music video" render (#6172): a song, the images Todd picked, a direction.

    image_paths is a JSON list of clips {"path", "sing", "prompt"}: an absolute
    path inside image_folder (the folder he chose in the UI; the router refuses
    anything else), whether that clip lip syncs to the song, and a per-clip
    Direction override. Older rows hold plain path strings; the worker reads both. The worker process
    (music_video/worker.py) moves status queued -> analyzing -> rendering ->
    stitching -> done | failed | cancelled.
    """

    __tablename__ = "music_video_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[int] = mapped_column(Integer, index=True)
    quality: Mapped[str] = mapped_column(String(10), default="draft")
    aspect: Mapped[str] = mapped_column(String(8), default="16:9")  # planner.ASPECTS key
    image_folder: Mapped[str] = mapped_column(Text)
    image_paths: Mapped[str] = mapped_column(Text)  # JSON list
    direction: Mapped[str] = mapped_column(Text, default="")
    prompt_template: Mapped[str | None] = mapped_column(Text, nullable=True)  # #6867, None = default
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    clips_total: Mapped[int] = mapped_column(Integer, default=0)
    clips_done: Mapped[int] = mapped_column(Integer, default=0)
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
