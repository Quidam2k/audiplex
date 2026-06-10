"""Music API — browse genres/artists/albums/tracks, playlists, stream + covers."""

import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from audiplex.config import get_settings, set_library_roots_for_category
from audiplex.auth import get_admin_user, get_current_user
from audiplex.database import get_db
from audiplex.models import (
    Album,
    Artist,
    Favorite,
    Playlist,
    PlaylistTrack,
    PlayStat,
    Track,
    User,
)
from audiplex.schemas import (
    AlbumDetail,
    AlbumSummary,
    ArtistDetail,
    ArtistSchema,
    FavoriteCreate,
    FavoriteSchema,
    FolderListing,
    FolderNode,
    GenreSchema,
    MusicRoot,
    MusicRootsResponse,
    MusicRootsUpdate,
    PlayStatEvent,
    PlayStatSchema,
    PlaylistAppend,
    PlaylistCreate,
    PlaylistDetail,
    PlaylistSummary,
    PlaylistUpdate,
    SkipSuspectSchema,
    TrackSchema,
)

# Entity types for the polymorphic Favorite table.
FAVORITE_TYPES = {"track", "album", "artist", "genre", "book", "to_read"}
# Tracks with stop events whose played_seconds is below this threshold are
# treated as early skips.
EARLY_SKIP_THRESHOLD_SECONDS = 10.0
from audiplex.utils.cover_art import get_album_cover_path
from audiplex.utils.streaming import EXT_MIME, serve_file

router = APIRouter(prefix="/api/music", tags=["music"])


def get_music_roots() -> list[str]:
    """Absolute paths of configured library roots with category 'music'.

    Exposed as a FastAPI dependency so tests can override it without
    touching the cached global settings.
    """
    return [r.path for r in get_settings().library_roots if r.category == "music"]


def _norm(p: str) -> str:
    """Normalize a folder path for comparison: forward slashes, no trailing /."""
    return p.replace("\\", "/").rstrip("/")


def _basename(p: str) -> str:
    return p.rsplit("/", 1)[-1] or p


def _is_within(child: str, parent: str) -> bool:
    """True if `child` is `parent` or nested beneath it (separator-agnostic)."""
    c, pa = _norm(child), _norm(parent)
    return c == pa or c.startswith(pa + "/")


def _album_summary(album: Album) -> AlbumSummary:
    s = AlbumSummary.model_validate(album)
    s.artist_name = album.artist.name if album.artist else None
    return s


def _track_schema(track: Track) -> TrackSchema:
    s = TrackSchema.model_validate(track)
    s.artist_name = track.artist.name if track.artist else None
    return s


@router.get("/genres", response_model=list[GenreSchema])
def list_genres(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (
        db.query(Album.genre, func.count(Album.id))
        .filter(Album.genre.is_not(None))
        .group_by(Album.genre)
        .order_by(Album.genre)
        .all()
    )
    return [GenreSchema(name=name, album_count=count) for name, count in rows]


@router.get("/genres/{genre_name}/tracks", response_model=list[TrackSchema])
def get_genre_tracks(genre_name: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Flat track list for a genre, ordered by artist → album → disc → track."""
    tracks = (
        db.query(Track)
        .join(Album, Track.album_id == Album.id)
        .join(Artist, Track.artist_id == Artist.id)
        .options(selectinload(Track.artist))
        .filter(Album.genre == genre_name)
        .order_by(
            func.lower(Artist.name),
            func.lower(Album.title),
            Track.disc_number,
            Track.track_number,
        )
        .all()
    )
    return [_track_schema(t) for t in tracks]


@router.get("/artists", response_model=list[ArtistSchema])
def list_artists(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.query(Artist).order_by(Artist.name).all()


@router.get("/artists/{artist_id}", response_model=ArtistDetail)
def get_artist(artist_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    artist = (
        db.query(Artist)
        .options(selectinload(Artist.albums).selectinload(Album.artist))
        .filter(Artist.id == artist_id)
        .first()
    )
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    detail = ArtistDetail.model_validate(artist)
    detail.albums = [
        _album_summary(a)
        for a in sorted(artist.albums, key=lambda x: (x.title or "").lower())
    ]
    return detail


@router.get("/artists/{artist_id}/tracks", response_model=list[TrackSchema])
def get_artist_tracks(artist_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Flat track list for an artist, ordered by album → disc → track number.

    Used by the Android client's "Play all" / "Shuffle all" on the artist
    detail screen so it can queue every track from every album in one shot.
    """
    if not db.query(Artist).filter(Artist.id == artist_id).first():
        raise HTTPException(status_code=404, detail="Artist not found")
    tracks = (
        db.query(Track)
        .join(Album, Track.album_id == Album.id)
        .options(selectinload(Track.artist))
        .filter(Track.artist_id == artist_id)
        .order_by(
            func.lower(Album.title),
            Track.disc_number,
            Track.track_number,
        )
        .all()
    )
    return [_track_schema(t) for t in tracks]


@router.get("/albums", response_model=list[AlbumSummary])
def list_albums(
    genre: str | None = Query(None),
    artist_id: int | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Album).options(selectinload(Album.artist))
    if genre:
        query = query.filter(Album.genre == genre)
    if artist_id is not None:
        query = query.filter(Album.artist_id == artist_id)
    albums = query.order_by(Album.title).all()
    return [_album_summary(a) for a in albums]


@router.get("/albums/{album_id}", response_model=AlbumDetail)
def get_album(album_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    album = (
        db.query(Album)
        .options(
            selectinload(Album.artist),
            selectinload(Album.tracks).selectinload(Track.artist),
        )
        .filter(Album.id == album_id)
        .first()
    )
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    detail = AlbumDetail.model_validate(album)
    detail.artist_name = album.artist.name if album.artist else None
    detail.tracks = [
        _track_schema(t)
        for t in sorted(album.tracks, key=lambda t: (t.disc_number, t.track_number))
    ]
    return detail


@router.get("/folders", response_model=FolderListing)
def list_folders(
    path: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    music_roots: list[str] = Depends(get_music_roots),
):
    """Browse the music tree derived from album folder paths.

    `path` omitted → the music roots that contain albums. Otherwise the
    folder's immediate child folders plus albums living directly in it.
    A path is only browsable if it's a music root or an ancestor of a
    scanned album, so this can't be used to walk arbitrary directories.
    """
    roots = [_norm(r) for r in music_roots]
    albums = db.query(Album).options(selectinload(Album.artist)).all()

    if path is None:
        nodes = []
        for root in roots:
            within = [a for a in albums if _is_within(a.folder_path, root)]
            if within:
                nodes.append(FolderNode(
                    name=_basename(root),
                    path=root,
                    album_count=len(within),
                    track_count=sum(a.track_count for a in within),
                ))
        return FolderListing(
            path=None, parent=None, folders=_sorted_nodes(nodes), albums=[]
        )

    current = _norm(path)
    within_current = [a for a in albums if _is_within(a.folder_path, current)]
    is_root = current in roots
    if not within_current and not is_root:
        raise HTTPException(status_code=404, detail="Folder not found")

    children: dict[str, list[Album]] = {}
    direct: list[Album] = []
    for a in within_current:
        an = _norm(a.folder_path)
        if an == current:
            direct.append(a)
            continue
        seg = an[len(current) + 1:].split("/", 1)[0]
        children.setdefault(f"{current}/{seg}", []).append(a)

    folders = [
        FolderNode(
            name=_basename(cp),
            path=cp,
            album_count=len(al),
            track_count=sum(x.track_count for x in al),
        )
        for cp, al in children.items()
    ]

    parent = None if is_root else current.rsplit("/", 1)[0] or None
    return FolderListing(
        path=current,
        parent=parent,
        folders=_sorted_nodes(folders),
        albums=[
            _album_summary(a)
            for a in sorted(direct, key=lambda x: (x.title or "").lower())
        ],
    )


@router.get("/folders/tracks", response_model=list[TrackSchema])
def get_folder_tracks(
    path: str = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Flat track list for every album under `path` (recursive).

    Ordered by folder path → disc → track so a folder turns into a
    natural playback/playlist order. Returns [] for an unknown path.
    """
    tracks = (
        db.query(Track)
        .join(Album, Track.album_id == Album.id)
        .options(selectinload(Track.album), selectinload(Track.artist))
        .all()
    )
    selected = [t for t in tracks if t.album and _is_within(t.album.folder_path, path)]
    selected.sort(
        key=lambda t: (_norm(t.album.folder_path), t.disc_number, t.track_number)
    )
    return [_track_schema(t) for t in selected]


def _sorted_nodes(nodes: list[FolderNode]) -> list[FolderNode]:
    return sorted(nodes, key=lambda n: n.name.lower())


def _roots_response(paths: list[str]) -> MusicRootsResponse:
    return MusicRootsResponse(
        roots=[MusicRoot(path=p, exists=os.path.isdir(p)) for p in paths]
    )


@router.get("/roots", response_model=MusicRootsResponse)
def list_music_roots(
    user: User = Depends(get_current_user),
    music_roots: list[str] = Depends(get_music_roots),
):
    """The folders that populate the Music section, each flagged with whether
    it currently exists on disk."""
    return _roots_response(music_roots)


@router.put("/roots", response_model=MusicRootsResponse)
def update_music_roots(
    body: MusicRootsUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_admin_user),
):
    """Replace the set of music folders, then rescan so the new folders show up.

    Paths are trimmed and de-duplicated (order preserved); blanks are dropped.
    Audiobook roots are left untouched.
    """
    from audiplex.scanner import scan_library

    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in body.paths:
        p = raw.strip()
        key = _norm(p).lower()
        if not p or key in seen:
            continue
        seen.add(key)
        cleaned.append(p)

    set_library_roots_for_category("music", cleaned)

    settings = get_settings()
    scan_library(db, settings.library_roots, settings.cover_cache_dir)

    return _roots_response(cleaned)


@router.get("/tracks/{track_id}", response_model=TrackSchema)
def get_track(track_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    track = (
        db.query(Track)
        .options(selectinload(Track.artist))
        .filter(Track.id == track_id)
        .first()
    )
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    return _track_schema(track)


@router.get("/stream/track/{track_id}")
def stream_track(track_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track or not os.path.exists(track.file_path):
        raise HTTPException(status_code=404, detail="Track not found")
    ext = os.path.splitext(track.file_path)[1].lower()
    return serve_file(track.file_path, EXT_MIME.get(ext, "application/octet-stream"), request)


@router.get("/covers/album/{album_id}")
def album_cover(album_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not db.query(Album).filter(Album.id == album_id).first():
        raise HTTPException(status_code=404, detail="Album not found")
    settings = get_settings()
    path = get_album_cover_path(album_id, settings.cover_cache_dir)
    if not path:
        raise HTTPException(status_code=404, detail="No cover available")
    media_type = "image/jpeg" if path.endswith(".jpg") else "image/png"
    return FileResponse(path, media_type=media_type)


@router.post("/stats", response_model=PlayStatSchema)
def post_stat(event: PlayStatEvent, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    stat = PlayStat(
        track_id=event.track_id,
        user_id=user.id,
        event=event.event,
        played_seconds=event.played_seconds,
    )
    db.add(stat)
    db.commit()
    db.refresh(stat)
    return stat


@router.get("/playlists", response_model=list[PlaylistSummary])
def list_playlists(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    playlists = (
        db.query(Playlist)
        .options(selectinload(Playlist.entries))
        .filter(Playlist.user_id == user.id)
        .order_by(Playlist.name)
        .all()
    )
    result = []
    for p in playlists:
        s = PlaylistSummary.model_validate(p)
        s.track_count = len(p.entries)
        result.append(s)
    return result


def _get_playlist_detail(playlist_id: int, user_id: int, db: Session) -> PlaylistDetail:
    playlist = (
        db.query(Playlist)
        .options(
            selectinload(Playlist.entries)
            .selectinload(PlaylistTrack.track)
            .selectinload(Track.artist),
        )
        .filter(Playlist.id == playlist_id, Playlist.user_id == user_id)
        .first()
    )
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    detail = PlaylistDetail.model_validate(playlist)
    detail.track_count = len(playlist.entries)
    detail.tracks = [
        _track_schema(entry.track)
        for entry in sorted(playlist.entries, key=lambda e: e.position)
        if entry.track
    ]
    return detail


@router.get("/playlists/{playlist_id}", response_model=PlaylistDetail)
def get_playlist(playlist_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _get_playlist_detail(playlist_id, user.id, db)


def _validate_track_ids(db: Session, track_ids: list[int]) -> None:
    """Raise 400 if any track_id doesn't exist."""
    if not track_ids:
        return
    found = {
        tid for (tid,) in db.query(Track.id).filter(Track.id.in_(track_ids)).all()
    }
    missing = [tid for tid in track_ids if tid not in found]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Track id(s) not found: {missing}",
        )


@router.post("/playlists", response_model=PlaylistDetail)
def create_playlist(body: PlaylistCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _validate_track_ids(db, body.track_ids)
    playlist = Playlist(name=body.name, user_id=user.id)
    db.add(playlist)
    db.flush()
    for pos, tid in enumerate(body.track_ids):
        db.add(PlaylistTrack(playlist_id=playlist.id, track_id=tid, position=pos))
    db.commit()
    return _get_playlist_detail(playlist.id, user.id, db)


@router.put("/playlists/{playlist_id}", response_model=PlaylistDetail)
def update_playlist(playlist_id: int, body: PlaylistUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id, Playlist.user_id == user.id).first()
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")

    if body.name is not None:
        playlist.name = body.name

    if body.track_ids is not None:
        _validate_track_ids(db, body.track_ids)
        db.query(PlaylistTrack).filter(
            PlaylistTrack.playlist_id == playlist_id
        ).delete(synchronize_session=False)
        db.flush()
        for pos, tid in enumerate(body.track_ids):
            db.add(PlaylistTrack(playlist_id=playlist_id, track_id=tid, position=pos))

    db.commit()
    return _get_playlist_detail(playlist_id, user.id, db)


@router.delete("/playlists/{playlist_id}")
def delete_playlist(playlist_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id, Playlist.user_id == user.id).first()
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    db.delete(playlist)
    db.commit()
    return {"deleted": playlist_id}


@router.post("/playlists/{playlist_id}/tracks", response_model=PlaylistDetail)
def append_to_playlist(
    playlist_id: int,
    body: PlaylistAppend,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Append tracks to the end of an existing playlist."""
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id, Playlist.user_id == user.id).first()
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    _validate_track_ids(db, body.track_ids)
    next_position = (
        db.query(func.coalesce(func.max(PlaylistTrack.position), -1))
        .filter(PlaylistTrack.playlist_id == playlist_id)
        .scalar()
    ) + 1
    for offset, tid in enumerate(body.track_ids):
        db.add(
            PlaylistTrack(
                playlist_id=playlist_id,
                track_id=tid,
                position=next_position + offset,
            )
        )
    db.commit()
    return _get_playlist_detail(playlist_id, user.id, db)


@router.get("/favorites", response_model=list[FavoriteSchema])
def list_favorites(
    entity_type: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Favorite).filter(Favorite.user_id == user.id)
    if entity_type:
        if entity_type not in FAVORITE_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown entity_type: {entity_type}")
        query = query.filter(Favorite.entity_type == entity_type)
    return query.order_by(Favorite.created_at.desc()).all()


@router.post("/favorites", response_model=FavoriteSchema)
def add_favorite(body: FavoriteCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if body.entity_type not in FAVORITE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown entity_type: {body.entity_type}")
    existing = (
        db.query(Favorite)
        .filter(
            Favorite.user_id == user.id,
            Favorite.entity_type == body.entity_type,
            Favorite.entity_key == body.entity_key,
        )
        .first()
    )
    if existing:
        return existing
    fav = Favorite(user_id=user.id, entity_type=body.entity_type, entity_key=body.entity_key)
    db.add(fav)
    db.commit()
    db.refresh(fav)
    return fav


@router.delete("/favorites/{entity_type}/{entity_key}")
def remove_favorite(entity_type: str, entity_key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if entity_type not in FAVORITE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown entity_type: {entity_type}")
    deleted = (
        db.query(Favorite)
        .filter(
            Favorite.user_id == user.id,
            Favorite.entity_type == entity_type,
            Favorite.entity_key == entity_key,
        )
        .delete()
    )
    db.commit()
    return {"deleted": deleted}


@router.get("/most-played", response_model=list[TrackSchema])
def list_most_played(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Top-N tracks by completed-play count.

    A "play" is a PlayStat with event='complete' OR a 'stop' with
    played_seconds beyond the early-skip threshold — so genuine listens
    count even when the user stops before the track auto-advances.
    """
    rows = (
        db.query(Track, func.count(PlayStat.id).label("plays"))
        .join(PlayStat, PlayStat.track_id == Track.id)
        .filter(
            PlayStat.user_id == user.id,
            (PlayStat.event == "complete")
            | (
                (PlayStat.event == "stop")
                & (PlayStat.played_seconds >= EARLY_SKIP_THRESHOLD_SECONDS)
            ),
        )
        .options(selectinload(Track.artist))
        .group_by(Track.id)
        .order_by(func.count(PlayStat.id).desc(), Track.title)
        .limit(limit)
        .all()
    )
    return [_track_schema(t) for t, _ in rows]


@router.get("/likely-skips", response_model=list[SkipSuspectSchema])
def list_likely_skips(
    limit: int = Query(25, ge=1, le=200),
    min_skips: int = Query(2, ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Tracks frequently abandoned in the first few seconds.

    A track is counted as an early skip when the client posts a 'stop'
    PlayStat with played_seconds below EARLY_SKIP_THRESHOLD_SECONDS, or
    when the next track starts via SEEK while the previous was still in
    its first few seconds (recorded as event='skip').
    """
    early_skip_filter = (
        (PlayStat.event == "skip")
        & (PlayStat.played_seconds < EARLY_SKIP_THRESHOLD_SECONDS)
    ) | (
        (PlayStat.event == "stop")
        & (PlayStat.played_seconds < EARLY_SKIP_THRESHOLD_SECONDS)
    )
    skips_subq = (
        db.query(
            PlayStat.track_id.label("tid"),
            func.count(PlayStat.id).label("early_skips"),
        )
        .filter(PlayStat.user_id == user.id, early_skip_filter)
        .group_by(PlayStat.track_id)
        .having(func.count(PlayStat.id) >= min_skips)
        .subquery()
    )
    starts_subq = (
        db.query(
            PlayStat.track_id.label("tid"),
            func.count(PlayStat.id).label("starts"),
        )
        .filter(PlayStat.user_id == user.id, PlayStat.event == "start")
        .group_by(PlayStat.track_id)
        .subquery()
    )
    rows = (
        db.query(
            Track,
            skips_subq.c.early_skips,
            func.coalesce(starts_subq.c.starts, 0),
        )
        .join(skips_subq, skips_subq.c.tid == Track.id)
        .outerjoin(starts_subq, starts_subq.c.tid == Track.id)
        .options(selectinload(Track.artist))
        .order_by(skips_subq.c.early_skips.desc(), Track.title)
        .limit(limit)
        .all()
    )
    return [
        SkipSuspectSchema(
            track=_track_schema(t),
            early_skip_count=int(skips),
            total_starts=int(starts or 0),
        )
        for t, skips, starts in rows
    ]
