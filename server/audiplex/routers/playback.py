"""DJ playback command bus + now-playing state (remote control, v1).

Endpoints:
  POST /api/playback/command       — agent enqueues a command
  GET  /api/playback/command/next  — client long-polls for the next command
  POST /api/playback/state         — client reports now-playing
  GET  /api/playback/state         — agent reads now-playing
  GET  /api/playback/playlists              — owner's playlists (read-only)
  GET  /api/playback/playlists/{id}         — owner's playlist detail
  GET  /api/playback/favorites              — owner's favorites (read-only)

All require a valid Bearer token (get_current_user). v1 is single-device, so
the bus is global: agent and device share one queue + one state regardless of
which account's token they present (see playback_bus for the rationale).
The playlists/favorites reads apply the same rationale: agents DJ the
configured owner's library (settings.dj_owner_username) regardless of which
service token they present, since /api/music/playlists and /api/music/
favorites are per-caller and a service account like dj-agent has none of
its own.

Transport = long-poll (locked decision): the GET blocks up to ~25s (under the
client's 30s read timeout) and returns 204 on timeout, at which point the
client simply re-issues.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, selectinload

from audiplex.auth import get_current_user
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import Favorite, Playlist, User
from audiplex.playback_bus import bus
from audiplex.routers.music import FAVORITE_TYPES, _get_playlist_detail
from audiplex.schemas import (
    FavoriteSchema,
    PlaybackCommand,
    PlaybackCommandQueued,
    PlaybackState,
    PlaylistDetail,
    PlaylistSummary,
)

router = APIRouter(prefix="/api/playback", tags=["playback"])

LONGPOLL_TIMEOUT_SECONDS = 25.0


def _resolve_owner(db: Session) -> User:
    """The human whose library agents DJ, per settings.dj_owner_username —
    not the caller (a service account like dj-agent has no library of its own)."""
    owner_username = get_settings().dj_owner_username
    owner = db.query(User).filter(User.username == owner_username).first()
    if not owner:
        raise HTTPException(
            status_code=404,
            detail=f"Configured DJ owner '{owner_username}' not found",
        )
    return owner


@router.post("/command", response_model=PlaybackCommandQueued)
async def post_command(cmd: PlaybackCommand, user: User = Depends(get_current_user)):
    rec = await bus.enqueue(cmd.type, cmd.payload)
    return PlaybackCommandQueued(id=rec.id, type=rec.type, pending=bus.pending())


@router.get("/command/next")
async def next_command(user: User = Depends(get_current_user)):
    """Long-poll for the next command. 204 (empty) on timeout — re-issue."""
    rec = await bus.next(LONGPOLL_TIMEOUT_SECONDS)
    if rec is None:
        return Response(status_code=204)
    return JSONResponse(
        {
            "id": rec.id,
            "type": rec.type,
            "payload": rec.payload,
            "created_at": rec.created_at,
        }
    )


@router.post("/state", response_model=PlaybackState)
def post_state(state: PlaybackState, user: User = Depends(get_current_user)):
    bus.set_state(state.model_dump())
    return state


@router.get("/state")
def get_state(user: User = Depends(get_current_user)):
    return bus.get_state() or {
        "playing": False,
        "track": None,
        "position_ms": 0,
        "duration_ms": 0,
        "queue_length": 0,
        "queue_index": 0,
        "queue": [],
        "volume": None,
        "updated_at": None,
    }


@router.get("/playlists", response_model=list[PlaylistSummary])
def list_owner_playlists(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    owner = _resolve_owner(db)
    playlists = (
        db.query(Playlist)
        .options(selectinload(Playlist.entries))
        .filter(Playlist.user_id == owner.id)
        .order_by(Playlist.name)
        .all()
    )
    result = []
    for p in playlists:
        s = PlaylistSummary.model_validate(p)
        s.track_count = len(p.entries)
        result.append(s)
    return result


@router.get("/playlists/{playlist_id}", response_model=PlaylistDetail)
def get_owner_playlist(
    playlist_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    owner = _resolve_owner(db)
    return _get_playlist_detail(playlist_id, owner.id, db)


@router.get("/favorites", response_model=list[FavoriteSchema])
def list_owner_favorites(
    entity_type: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    owner = _resolve_owner(db)
    query = db.query(Favorite).filter(Favorite.user_id == owner.id)
    if entity_type:
        if entity_type not in FAVORITE_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown entity_type: {entity_type}")
        query = query.filter(Favorite.entity_type == entity_type)
    return query.order_by(Favorite.created_at.desc()).all()
