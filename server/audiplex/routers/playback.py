"""DJ playback command bus + now-playing state (remote control, v1).

Endpoints:
  POST /api/playback/command       — agent enqueues a command
  GET  /api/playback/command/next  — client long-polls for the next command
  POST /api/playback/command/{id}/ack — client reports what it did with one
  GET  /api/playback/commands      — recent commands + their delivery status
  POST /api/playback/state         — client reports now-playing
  GET  /api/playback/state         — agent reads now-playing
  GET  /api/playback/device        — device liveness (last poll / state / command)
  POST /api/playback/client-log    — client ships a diagnostic up
  GET  /api/playback/client-log    — agent reads recent client diagnostics
  GET  /api/playback/client-exits  — process-exit reports, persisted to disk
  GET  /api/playback/link-history  — link gaps/resumes, persisted to disk
  GET  /api/playback/most-played            — owner's most-played (read-only)
  GET  /api/playback/likely-skips           — owner's early-skip suspects
  GET  /api/playback/playlists              — owner's playlists (read-only)
  GET  /api/playback/playlists/{id}         — owner's playlist detail
  GET  /api/playback/favorites              — owner's favorites (read-only)
  GET  /api/playback/ratings                — owner's star ratings (read-only)
  GET  /api/playback/track-stats            — owner's completion rates / skip positions
  GET  /api/playback/tracks/{id}/identity   — recording + work keys for a track
  GET  /api/playback/cooldown               — what the owner heard recently
  POST /api/playback/candidates/filter      — advisory: which picks repeat, and why

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

from audiplex import taste
from audiplex.auth import get_current_user
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import Favorite, Playlist, TrackRating, User
from audiplex.playback_bus import bus, read_link_history, read_persisted_exits
from audiplex.routers.music import (
    FAVORITE_TYPES,
    _get_playlist_detail,
    _tracks_by_id,
    likely_skips_for,
    most_played_for,
    recording_stats_view,
    track_identity_for,
)
from audiplex.schemas import (
    CandidateFilterRequest,
    CandidateFilterResult,
    ClientLogEntry,
    CooldownStateSchema,
    RecentPlaySchema,
    RecordingStatsSchema,
    SuppressionSchema,
    TrackIdentitySchema,
    PlaybackCommandAck,
    PlaybackCommandAckResult,
    FavoriteSchema,
    PlaybackCommand,
    PlaybackCommandQueued,
    PlaybackState,
    PlaylistDetail,
    PlaylistSummary,
    SkipSuspectSchema,
    TrackRatingSchema,
    TrackSchema,
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
            # Redeliveries are expected under at-least-once; the client dedupes
            # on id and this tells it (and us) which pass it is looking at.
            "delivery_count": rec.delivery_count,
        }
    )


@router.post("/command/{command_id}/ack", response_model=PlaybackCommandAckResult)
def ack_command(
    command_id: int,
    ack: PlaybackCommandAck,
    user: User = Depends(get_current_user),
):
    """The device reports what it actually did with a command.

    This is the half that was missing on 2026-08-14: the command was taken off
    the queue and then silently dropped, and nothing anywhere could tell the
    difference between that and a command still in flight. An ack — including
    a FAILING one — ends the ambiguity, and it is what stops redelivery.
    """
    rec = bus.ack(command_id, ack.status, ack.detail)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Unknown command {command_id}")
    return PlaybackCommandAckResult(**rec.summary())


@router.get("/commands")
def list_commands(
    limit: int = Query(20, ge=1, le=200), user: User = Depends(get_current_user)
):
    """Recent commands and what became of each — the DJ's delivery receipt."""
    return bus.commands(limit)


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


@router.get("/device")
def get_device(user: User = Depends(get_current_user)):
    """Device liveness: is a player actually out there listening?

    Distinct from /state, which only says what was last *played*. A device can
    be connected and idle (polling, nothing loaded) or gone entirely, and those
    two produced identical /state responses before this existed (#2961).
    """
    return bus.device_status()


@router.post("/client-log")
def post_client_log(entry: ClientLogEntry, user: User = Depends(get_current_user)):
    """Client-shipped diagnostic (player error, process-exit reason, etc.)."""
    return bus.add_client_log(entry.model_dump())


@router.get("/client-log")
def get_client_log(
    limit: int = Query(50, ge=1, le=200), user: User = Depends(get_current_user)
):
    return bus.client_log(limit)


@router.get("/client-exits")
def get_client_exits(
    limit: int = Query(50, ge=1, le=200), user: User = Depends(get_current_user)
):
    """Process-exit reports from disk — the ones that survive a restart.

    The ring buffer above is memory-only, and the phone advances its report
    watermark the moment we accept an entry, so a restart would otherwise
    destroy a death report nobody had read yet (#3021).
    """
    return read_persisted_exits(limit)


@router.get("/link-history")
def get_link_history(
    limit: int = Query(50, ge=1, le=200), user: User = Depends(get_current_user)
):
    """When the DJ link dropped, and when it came back.

    Durable because the live view is not: last-poll is a single in-memory
    float, so "did the link hold overnight?" was unanswerable on 2026-08-18
    even with the foreground service running (#3031). A `resumed` entry with
    after_restart means the SERVER restarted — not a device fault, and
    deliberately not recorded as a gap.
    """
    return read_link_history(limit)


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


@router.get("/ratings", response_model=list[TrackRatingSchema])
def list_owner_ratings(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """The owner's star ratings, best first (#3024).

    Owner-scoped for the same reason the playlists and favorites reads above
    are: the DJ authenticates as dj-agent, which has never rated anything, so
    a per-caller read would hand it an empty list and it would conclude Todd
    has no opinions rather than that it was looking at the wrong account.
    """
    owner = _resolve_owner(db)
    return (
        db.query(TrackRating)
        .filter(TrackRating.user_id == owner.id)
        .order_by(TrackRating.rating.desc(), TrackRating.updated_at.desc())
        .all()
    )


@router.get("/most-played", response_model=list[TrackSchema])
def list_owner_most_played(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The owner's most-played tracks (#3028).

    Owner-scoped for the same reason as /ratings above, and this one was a
    known-broken read rather than a hypothetical: /api/music/most-played
    filters by the CALLER, the DJ authenticates as dj-agent, and dj-agent has
    never played anything — so dj_taste's read was always an empty list, and
    the DJ would conclude Todd has no history rather than that it was asking
    as the wrong account.
    """
    owner = _resolve_owner(db)
    return most_played_for(db, owner.id, limit)


@router.get("/likely-skips", response_model=list[SkipSuspectSchema])
def list_owner_likely_skips(
    limit: int = Query(25, ge=1, le=200),
    min_skips: int = Query(2, ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The owner's early-skip suspects — same scoping story as most-played."""
    owner = _resolve_owner(db)
    return likely_skips_for(db, owner.id, limit, min_skips)


# ----- Taste aggregation + recency cooldown, owner-scoped (#947/#948) -----


@router.get("/track-stats", response_model=list[RecordingStatsSchema])
def list_owner_track_stats(
    limit: int = Query(50, ge=1, le=500),
    min_starts: int = Query(1, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Completion rates and skip positions for the OWNER (#947).

    completion_rate separates "played eight times, finished twice" from
    "played twice, finished twice" — raw play counts read the same for both.
    mean/median skip position says WHERE it loses him, which is the difference
    between a song he dislikes and one that is simply too long.

    Owner-scoped for the reason in [list_owner_most_played] (#3028): a service
    account has no listening history, so a per-caller read hands the DJ an
    empty list forever.
    """
    owner = _resolve_owner(db)
    return recording_stats_view(db, owner.id, limit, min_starts)


@router.get("/tracks/{track_id}/identity", response_model=TrackIdentitySchema)
def get_owner_track_identity(
    track_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """The recording and work keys for one track, plus what shares them."""
    identity = track_identity_for(db, track_id)
    if identity is None:
        raise HTTPException(status_code=404, detail="Track not found")
    return identity


def _cooldown_settings(
    recording_minutes: float | None, work_minutes: float | None
) -> tuple[float, float]:
    """Caller's override, else the configured default. Tunable both ways —
    the twenty minutes is Todd's number, not a law."""
    settings = get_settings()
    return (
        float(
            recording_minutes
            if recording_minutes is not None
            else settings.dj_recording_cooldown_minutes
        ),
        float(
            work_minutes
            if work_minutes is not None
            else settings.dj_work_cooldown_minutes
        ),
    )


@router.get("/cooldown", response_model=CooldownStateSchema)
def get_owner_cooldown(
    recording_cooldown_minutes: float | None = Query(None, ge=0, le=1440),
    work_cooldown_minutes: float | None = Query(None, ge=0, le=1440),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """What the owner has heard inside the cooldown window (#948).

    Needs no new storage: PlayStat already carries timestamps, so recency is
    a query over history that has been accumulating all along.
    """
    owner = _resolve_owner(db)
    recording_minutes, work_minutes = _cooldown_settings(
        recording_cooldown_minutes, work_cooldown_minutes
    )
    plays = taste.recent_plays_for(
        db, owner.id, max(recording_minutes, work_minutes)
    )
    tracks = _tracks_by_id(db, [play.track_id for play in plays])
    return CooldownStateSchema(
        recording_cooldown_minutes=recording_minutes,
        work_cooldown_minutes=work_minutes,
        recent_plays=[
            RecentPlaySchema(
                track_id=play.track_id,
                recording_id=play.recording_id,
                work_id=play.work_id,
                event=play.event,
                at=play.at,
                minutes_ago=play.minutes_ago,
                title=tracks[play.track_id].title if play.track_id in tracks else None,
                artist_name=(
                    tracks[play.track_id].artist.name
                    if play.track_id in tracks and tracks[play.track_id].artist
                    else None
                ),
            )
            for play in plays
        ],
    )


@router.post("/candidates/filter", response_model=CandidateFilterResult)
def filter_owner_candidates(
    body: CandidateFilterRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Which of these picks would repeat something the owner just heard (#948).

    ADVISORY. It returns an opinion; it does not queue, block, or alter
    playback. When Todd asks for a song by name he gets that song — cooldown
    exists to stop the DJ repeating ITSELF. Suppressed candidates come back
    with a reason attached rather than silently vanishing, so the DJ can say
    why it passed over something instead of quietly narrowing its own world.
    """
    owner = _resolve_owner(db)
    recording_minutes, work_minutes = _cooldown_settings(
        body.recording_cooldown_minutes, body.work_cooldown_minutes
    )
    verdict = taste.filter_candidates(
        db,
        owner.id,
        body.track_ids,
        recording_minutes,
        work_minutes,
        body.min_rating,
    )
    tracks = _tracks_by_id(db, [s.track_id for s in verdict.suppressed])
    return CandidateFilterResult(
        allowed=verdict.allowed,
        suppressed=[
            SuppressionSchema(
                track_id=s.track_id,
                reason=s.reason,
                detail=s.detail,
                minutes_ago=s.minutes_ago,
                clears_in_minutes=s.clears_in_minutes,
                title=tracks[s.track_id].title if s.track_id in tracks else None,
                artist_name=(
                    tracks[s.track_id].artist.name
                    if s.track_id in tracks and tracks[s.track_id].artist
                    else None
                ),
            )
            for s in verdict.suppressed
        ],
        recording_cooldown_minutes=verdict.recording_cooldown_minutes,
        work_cooldown_minutes=verdict.work_cooldown_minutes,
    )
