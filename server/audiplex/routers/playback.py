"""DJ playback command bus + now-playing state (remote control, v1).

Endpoints:
  POST /api/playback/command       — agent enqueues a command
  GET  /api/playback/command/next  — client long-polls for the next command
  POST /api/playback/command/{id}/ack — client reports what it did with one
  GET  /api/playback/commands      — recent commands + their delivery status
  POST /api/playback/state         — client reports now-playing
  GET  /api/playback/state         — agent reads now-playing
  GET  /api/playback/devices       — registered renderers + which is active
  POST /api/playback/devices/{id}/activate — transfer playback to a device
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
  POST /api/playback/mix/plan               — dj_mix: trim, dedupe, shuffle the queue tail

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

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, selectinload

from audiplex import taste
from audiplex.identity import build_identity_map
from audiplex.mix import plan_mix, plan_summary
from audiplex.auth import get_current_user
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import Favorite, Playlist, TrackRating, User
from audiplex.playback_bus import LEGACY_DEVICE_ID, bus, read_link_history, read_persisted_exits
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
    MixPlanRequest,
    MixPlanResult,
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
async def next_command(
    request: Request,
    device_id: str | None = Query(None),
    device_name: str | None = Query(None),
    device_type: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    """Long-poll for the next command. 204 (empty) on timeout — re-issue.

    A paramless poll (today's Android app) is served exactly as before. A
    client that identifies itself with device_id joins the device registry and
    only receives commands while it is the active device (Spotify-Connect-style
    targeting); see playback_bus for the fallback rules.
    """
    if device_id in (None, LEGACY_DEVICE_ID):
        # The phone reaches us at ITS configured base URL; the chime bed player
        # needs that absolute URL (#5499). Relative clip URLs don't need it.
        from audiplex import dj_triggers

        dj_triggers.note_client_base_url(str(request.base_url))
    rec = await bus.next(
        LONGPOLL_TIMEOUT_SECONDS,
        device_id=device_id,
        device_name=device_name,
        device_type=device_type,
    )
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
def post_state(
    state: PlaybackState,
    device_id: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    # A state report is also a liveness beat: a client that identifies itself
    # keeps its device fresh between long-polls (rider R2 accuracy).
    if device_id:
        bus.touch_device(device_id)
    bus.set_state(state.model_dump(), device_id)
    return state


@router.get("/state")
def get_state(
    device_id: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    """Now-playing of `device_id`, or by default of whichever device is rendering."""
    return bus.get_state(device_id) or {
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


@router.get("/devices")
def list_devices(user: User = Depends(get_current_user)):
    """Every renderer that has announced itself, with liveness + which is active.

    This is how the DJ (and a follow-me client) learns what it can hand playback
    to. A device drops out of `connected` on its own once it stops polling.
    """
    return {"active_device_id": bus.active_device_id, "devices": bus.devices()}


@router.post("/devices/{device_id}/activate")
def activate_device(device_id: str, user: User = Depends(get_current_user)):
    """Transfer playback to `device_id` (Spotify-Connect handoff).

    The previous renderer is told to pause and report where it was; the new one
    then resumes that queue at that position (see PlaybackBus.transfer).
    """
    rec = bus.transfer(device_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
    return {"active_device_id": bus.active_device_id, "devices": bus.devices()}


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


@router.post("/mix/plan", response_model=MixPlanResult)
def plan_owner_mix(
    body: MixPlanRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Plan the queue tail for a dj_mix add — Todd's standing order (#2842).

    Pure planning: queues nothing. Dedupes on RECORDING identity so the same
    file in two folders is one entry, and counts as played both what the phone
    already got through this set (body.played_ids) and anything the owner heard
    inside the recording cooldown window, so a mix built after a restart still
    doesn't open with the song that just ended.
    """
    identities = build_identity_map(db)
    recording_minutes, _ = _cooldown_settings(None, None)
    owner = _resolve_owner(db)
    recent = taste.recent_plays_for(db, owner.id, recording_minutes, identities)
    skip = non_music(db, body.new_ids)  # #ride0928: no podcast/clip/ambient in a mix
    plan = plan_mix(
        body.current_id,
        list(body.played_ids) + [p.track_id for p in recent],
        body.upcoming_ids,
        [i for i in body.new_ids if i not in skip],
        {track_id: ident.recording_id for track_id, ident in identities.items()},
        shuffle=body.shuffle,
        seed=body.seed,
    )
    return MixPlanResult(
        upcoming=plan.upcoming,
        kept_from_queue=plan.kept_from_queue,
        added=plan.added,
        trimmed_played=plan.trimmed_played,
        trimmed_duplicates=plan.trimmed_duplicates,
        summary=plan_summary(plan),
    )


# ----- #3249: which track ids can actually stream -----
#
# 2026-09-28: a drive letter moved (E: -> H:) under 277 tracks. The stream route
# 404'd them, the phone ACKED play_now anyway, and Todd heard nothing. The DJ
# MCP asks here before it sends a track list, so a dead path is never queued.

from pydantic import BaseModel as _BaseModel  # noqa: E402  #3249


class PlayableRequest(_BaseModel):  # #3249
    track_ids: list[int] = []


class PlayableResult(_BaseModel):  # #3249
    playable: list[int]
    missing: list[int]


@router.post("/tracks/playable", response_model=PlayableResult)
def playable_tracks(
    body: PlayableRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Split track ids into ones whose file is on disk and ones that aren't (#3249).

    Order is preserved. An id with no track row counts as missing. Negative ids
    (DJ voice breaks) pass through untouched; they are not library files.
    """
    import os  # #3249
    from audiplex.models import Track  # #3249

    wanted = [i for i in body.track_ids if i > 0]
    paths = dict(db.query(Track.id, Track.file_path).filter(Track.id.in_(wanted)).all()) if wanted else {}
    playable: list[int] = []
    missing: list[int] = []
    for i in body.track_ids:
        if i <= 0 or (paths.get(i) and os.path.exists(paths[i])):
            playable.append(i)
        else:
            missing.append(i)
    return PlayableResult(playable=playable, missing=missing)


# ----- DJ Pool: persistent mix state (#5470, #5473, #5477, #5495) -----
#
# The pool lives in THIS process (get_pool() singleton) because the bus hook
# that tops it up runs here. The MCP server is a separate process and must go
# through these routes; a DJPool() it builds itself never reaches the bus.


def _spec_row(db: Session, name: str):
    from sqlalchemy import text

    return db.execute(
        text("SELECT id, name, request_text, sources_json, balance, ahead, "
             "exclude_recent_hours, notes_json FROM dj_mix_specs WHERE name = :name"),
        {"name": name},
    ).first()


def _spec_cues(db: Session, spec_id) -> list[dict]:
    """A spec's not-yet-fired cues, ready to become the pool's pending_cues."""
    from sqlalchemy import text

    if not spec_id:
        return []
    row = db.execute(
        text("SELECT notes_json FROM dj_mix_specs WHERE id = :id"), {"id": spec_id}
    ).first()
    notes = json.loads(row[0]) if row and row[0] else []
    return [
        # planned: a spec's cues never go stale on Todd speaking (#5544)
        {**n, "done": False, "held_boundaries": n.get("held_boundaries", 0), "planned": True}
        for n in notes
        if n.get("status", "pending") == "pending"
    ]


@router.get("/pool", tags=["dj_pool"])
def get_pool_status(user: User = Depends(get_current_user)):
    """Current DJ pool status: per-lane details, pending cues."""
    from audiplex.dj_pool import get_pool

    return get_pool().status()


@router.post("/pool", tags=["dj_pool"])
def set_pool(
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Start (or replace) the DJ pool.

    body: {spec_id?, lanes: {label: [track_ids]}, balance, ahead,
           exclude_recent_hours, starvation_config?, prime_current_id?}
    Legacy body {track_ids, source_labels} still works (one "default" lane).
    Lanes are resolved by the caller; a zero-track lane is kept, marked
    zero_on_resolve and exhausted. With spec_id, the spec's pending cues load.
    prime_current_id (the track playing now, 0 if nothing is loaded) runs one
    top-up immediately and returns its picks as initial_picks, so the caller
    can replace the upcoming queue once; after that the bus hook appends.
    """
    from audiplex.dj_pool import get_pool, lanes_from_ids

    pool = get_pool()
    spec_id = body.get("spec_id")
    balance = body.get("balance", "even")
    ahead = int(body.get("ahead", 4))
    exclude_recent_hours = body.get("exclude_recent_hours", 12)
    raw_lanes = body.get("lanes")
    if raw_lanes:
        lanes = {str(label): [int(t) for t in ids] for label, ids in raw_lanes.items()}
        skip = non_music(db, [t for ids in lanes.values() for t in ids])  # #ride0928
        lanes = {label: [t for t in ids if t not in skip] for label, ids in lanes.items()}
        track_ids = [t for ids in lanes.values() for t in ids]
        source_labels = {t: label for label, ids in lanes.items() for t in ids}
        result = pool.set_pool(
            spec_id, track_ids, source_labels, lanes=lanes_from_ids(lanes),
            balance=balance, ahead=ahead, exclude_recent_hours=exclude_recent_hours,
        )
    else:
        result = pool.set_pool(
            spec_id, body.get("track_ids", []), body.get("source_labels", {}),
            balance=balance, ahead=ahead, exclude_recent_hours=exclude_recent_hours,
        )
    if isinstance(body.get("starvation_config"), dict):
        pool.state["starvation_config"].update(body["starvation_config"])
    pool.state["pending_cues"] = _spec_cues(db, spec_id)
    pool._persist()

    prime = body.get("prime_current_id")
    if prime is not None:
        top = pool.top_up(
            current_track_id=int(prime),
            upcoming_track_ids=[int(prime)],
            current_played_track_ids=[int(t) for t in body.get("queued_ids") or []],
            db=db,
        )
        result["initial_picks"] = top.get("picks", [])
    return result


@router.delete("/pool", tags=["dj_pool"])
def stop_pool(user: User = Depends(get_current_user)):
    """Stop the DJ pool. stopped is true only if one was running."""
    from audiplex.dj_pool import get_pool

    return {"stopped": get_pool().stop()}


@router.post("/pool/outro", tags=["dj_pool"])
async def arm_pool_outro(body: dict, user: User = Depends(get_current_user)):
    """Arm a one-shot ride-end outro (#5515): a track_end cue on the CURRENT track
    that inserts the outro clip after it, then pauses once the clip has played.
    Never cuts a song. body: {clip_id} or {say} (rendered here with the DJ TTS),
    optional title, agent, duration_seconds.
    409 when nothing is playing; 503 when {say} cannot be rendered."""
    from audiplex import dj_triggers

    agent = body.get("agent")
    title = body.get("title") or (f"Ride outro · {agent}" if agent else "Ride outro")
    say = body.get("say")
    clip_id = body.get("clip_id")
    duration = body.get("duration_seconds")
    rendered_at = None
    st = bus.get_state() or {}
    track = st.get("track") or {}
    if not st.get("playing") or not isinstance(track.get("id"), int) or track["id"] <= 0:
        return JSONResponse({"armed": False, "reason": "nothing playing"}, status_code=409)
    if clip_id is None:
        if not isinstance(say, str) or not say.strip():
            raise HTTPException(status_code=400, detail="clip_id or say required")
        try:
            clip = await dj_triggers.render_say(say.strip(), title)
        except Exception as e:
            return JSONResponse({"armed": False, "reason": f"render failed: {e}"}, status_code=503)
        clip_id, duration, rendered_at = clip["clip_id"], clip["duration_seconds"], clip["rendered_at"]
    try:
        clip_id = int(clip_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="clip_id must be an integer")
    result = dj_triggers.arm_outro(
        bus, clip_id, title, duration_seconds=duration, rendered_at=rendered_at,
        agent=agent, say=say,
    )
    if not result.get("armed"):
        return JSONResponse(result, status_code=409)
    return result


@router.delete("/pool/outro", tags=["dj_pool"])
def disarm_pool_outro(user: User = Depends(get_current_user)):
    """Cancel an armed outro that has not paused yet (rider back on the bike)."""
    from audiplex import dj_triggers

    return {"disarmed": dj_triggers.disarm_outro()}


@router.patch("/pool/chimes", tags=["dj_pool"])
def set_pool_chimes(body: dict, user: User = Depends(get_current_user)):
    """Chime settings (#5499): {enabled?, volume? 0-1, hour_strikes?}."""
    from audiplex import dj_triggers

    vol = body.get("volume")
    if vol is not None and not isinstance(vol, (int, float)):
        raise HTTPException(status_code=400, detail="volume must be a number 0-1")
    return dj_triggers.set_chime_settings(
        enabled=body.get("enabled"), volume=vol, hour_strikes=body.get("hour_strikes"),
    )


# ----- DJ Specs: persistent mix specs with cues (#5477) -----


@router.post("/mix-specs", tags=["dj_specs"])
def create_mix_spec(
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create or replace a DJ mix spec.

    body: {name, request_text, sources, balance, ahead, exclude_recent_hours,
           notes_json?, last_counts?}
    """
    from sqlalchemy import text

    name = body.get("name", "")
    if not name:
        raise HTTPException(status_code=400, detail="name required")

    # request_text is a TEXT column; a structured value (the seed keeps Todd's
    # verbatim messages as a list) 500'd the insert (#5530), so encode it.
    request_text = body.get("request_text", "")
    if request_text is None:
        request_text = ""
    elif not isinstance(request_text, str):
        request_text = json.dumps(request_text)

    db.execute(text("DELETE FROM dj_mix_specs WHERE name = :name"), {"name": name})
    db.execute(
        text("""
            INSERT INTO dj_mix_specs
            (name, request_text, sources_json, balance, ahead, exclude_recent_hours,
             notes_json, last_counts_json, created_at, updated_at)
            VALUES (:name, :request_text, :sources_json, :balance, :ahead,
                    :exclude_recent_hours, :notes_json, :last_counts_json,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """),
        {
            "name": name,
            "request_text": request_text,
            "sources_json": json.dumps(body.get("sources", [])),
            "balance": body.get("balance", "even"),
            "ahead": body.get("ahead", 4),
            "exclude_recent_hours": body.get("exclude_recent_hours", 12),
            "notes_json": json.dumps(body.get("notes_json", [])),
            "last_counts_json": json.dumps(body.get("last_counts", {})),
        },
    )
    db.commit()
    row = _spec_row(db, name)
    return {"name": name, "id": row[0] if row else None, "status": "saved"}


@router.get("/mix-specs", tags=["dj_specs"])
def list_mix_specs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all saved mix specs."""
    from sqlalchemy import text

    result = db.execute(text("SELECT id, name, request_text, balance, ahead FROM dj_mix_specs ORDER BY name"))
    return [
        {"id": row[0], "name": row[1], "request_text": row[2], "balance": row[3], "ahead": row[4]}
        for row in result
    ]


@router.get("/mix-specs/{name}", tags=["dj_specs"])
def get_mix_spec(
    name: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get a specific mix spec by name."""
    row = _spec_row(db, name)
    if not row:
        raise HTTPException(status_code=404, detail=f"Spec '{name}' not found")
    return {
        "id": row[0],
        "name": row[1],
        "request_text": row[2],
        "sources": json.loads(row[3]) if row[3] else [],
        "balance": row[4],
        "ahead": row[5],
        "exclude_recent_hours": row[6],
        "notes": json.loads(row[7]) if row[7] else [],
    }


@router.patch("/mix-specs/{name}", tags=["dj_specs"])
def update_mix_spec(
    name: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Edit a spec's sources; re-sync the running pool if it is this spec's.

    body: {add_sources?: [src], remove_sources?: [query or label],
           lanes?: {label: [track_ids]}}
    `lanes` is the caller's resolution of the spec's sources AFTER the edit.
    When the active pool was started from this spec, its lanes are replaced
    with them (per-lane history kept for labels that survive).
    """
    from sqlalchemy import text
    from audiplex.dj_pool import get_pool

    row = _spec_row(db, name)
    if not row:
        raise HTTPException(status_code=404, detail=f"Spec '{name}' not found")
    spec_id = row[0]
    sources = json.loads(row[3]) if row[3] else []

    if body.get("add_sources"):
        sources.extend(body["add_sources"])
    if body.get("remove_sources"):
        gone = set(body["remove_sources"])
        sources = [s for s in sources if s.get("query") not in gone and s.get("label") not in gone]

    db.execute(
        text("UPDATE dj_mix_specs SET sources_json = :sources_json, "
             "updated_at = CURRENT_TIMESTAMP WHERE id = :id"),
        {"sources_json": json.dumps(sources), "id": spec_id},
    )
    db.commit()

    resynced = False
    pool = get_pool()
    lanes = body.get("lanes")
    if lanes is not None and pool.is_active() and pool.state.get("spec_id") == spec_id:
        pool.resync_lanes({str(k): [int(t) for t in v] for k, v in lanes.items()})
        resynced = True

    return {"name": name, "id": spec_id, "sources": sources, "status": "updated",
            "pool_resynced": resynced}


@router.post("/mix-specs/{name}/notes", tags=["dj_specs"])
def add_spec_note(
    name: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add a cue/note to a mix spec.

    body: {after_track_id?, play_track_id?, say?, trigger_kind?, clip_id?}
    If the running pool is this spec's, the cue is armed on it as well.
    """
    from sqlalchemy import text
    import time
    from audiplex.dj_pool import get_pool

    row = _spec_row(db, name)
    if not row:
        raise HTTPException(status_code=404, detail=f"Spec '{name}' not found")
    notes = json.loads(row[7]) if row[7] else []

    note = {
        "id": int(time.time() * 1000) % 1000000,
        "trigger": {
            "kind": body.get("trigger_kind", "track_end"),
            "track_id": body.get("after_track_id"),
        },
        "play_track": body.get("play_track_id"),
        "say": body.get("say"),
        "clip_id": body.get("clip_id"),
        "status": "pending",
    }
    # A pre-rendered patter clip (#5480): the cue fires the clip, never live speech.
    for key in ("clip_title", "clip_duration", "rendered_at", "voice", "agent"):
        if body.get(key) is not None:
            note[key] = body[key]
    if note["clip_id"] is not None:
        try:
            note["clip_id"] = int(note["clip_id"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="clip_id must be an integer")
    notes.append(note)

    db.execute(
        text("UPDATE dj_mix_specs SET notes_json = :notes_json, "
             "updated_at = CURRENT_TIMESTAMP WHERE id = :id"),
        {"notes_json": json.dumps(notes), "id": row[0]},
    )
    db.commit()

    pool = get_pool()
    if pool.is_active() and pool.state.get("spec_id") == row[0]:
        pool.state.setdefault("pending_cues", []).append({**note, "done": False, "held_boundaries": 0})
        pool._persist()
    return note


@router.get("/mix-specs/{name}/notes", tags=["dj_specs"])
def get_spec_notes(
    name: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get all cues/notes for a mix spec."""
    row = _spec_row(db, name)
    if not row:
        raise HTTPException(status_code=404, detail=f"Spec '{name}' not found")
    return {"name": name, "notes": json.loads(row[7]) if row[7] else []}


# ----- #ride0928: content kinds, play history, DJ pair notes, owner playlists -----

CONTENT_KINDS = ("music", "podcast", "clip", "ambient")  # #ride0928


def non_music(db: Session, track_ids) -> set[int]:  # #ride0928
    """The ids whose content_kind is NOT music. DJ mixes and pools drop them, so
    a podcast or an ambient bed never lands in a ride. Unknown ids aren't here."""
    from audiplex.models import Track

    ids = {int(i) for i in track_ids if int(i) > 0}
    if not ids:
        return set()
    rows = db.query(Track.id).filter(Track.id.in_(ids), Track.content_kind != "music").all()
    return {r[0] for r in rows}


@router.put("/content-kind", tags=["dj_library"])
def set_content_kind(body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Set content_kind on track_ids and/or every track under folder (#ride0928).

    body: {kind, track_ids?: [int], folder?: path}. Returns {kind, updated}.
    """
    import os
    from audiplex.models import Track

    kind = str(body.get("kind") or "")
    if kind not in CONTENT_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(CONTENT_KINDS)}")
    ids = [int(i) for i in body.get("track_ids") or []]
    folder = body.get("folder")
    if not ids and not folder:
        raise HTTPException(status_code=400, detail="give track_ids or folder")
    matched = db.query(Track).filter(Track.id.in_(ids)).all() if ids else []
    if folder:
        prefix = os.path.normcase(os.path.normpath(str(folder))).rstrip("\\/") + os.sep
        matched += [t for t in db.query(Track).all()
                    if os.path.normcase(os.path.normpath(t.file_path)).startswith(prefix)]
    seen = set()
    for t in matched:
        if t.id not in seen:
            seen.add(t.id)
            t.content_kind = kind
    db.commit()
    return {"kind": kind, "updated": len(seen)}


@router.get("/history", tags=["dj_library"])
def owner_history(
    limit: int = Query(20, ge=1, le=500),
    since: float | None = Query(None, description="epoch seconds"),
    events: str = Query("start", description="'start' (one row per play) or 'all'"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """What the OWNER played, newest first (#ride0928). Built on PlayStat.

    Owner-scoped like /most-played: the DJ asks as dj-agent, who never plays.
    """
    from datetime import datetime, timezone
    from audiplex.models import PlayStat, Track

    owner = _resolve_owner(db)
    q = db.query(PlayStat, Track).join(Track, Track.id == PlayStat.track_id).filter(
        PlayStat.user_id == owner.id
    )
    if events != "all":
        q = q.filter(PlayStat.event == "start")
    if since is not None:
        q = q.filter(PlayStat.timestamp >= datetime.fromtimestamp(since, timezone.utc).replace(tzinfo=None))
    rows = q.order_by(PlayStat.timestamp.desc(), PlayStat.id.desc()).limit(limit).all()
    out = []
    for ps, t in rows:
        ts = ps.timestamp
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append({
            "track_id": t.id,
            "title": t.title,
            "artist_name": t.artist.name if t.artist else None,
            "event": ps.event,
            "played_seconds": ps.played_seconds,
            "at": ts.timestamp() if ts else None,
        })
    return out


@router.get("/pair-notes", tags=["dj_library"])
def get_pair_notes(
    track_a: int | None = Query(None),
    track_b: int | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """DJ notes, newest first (#ride0928). track_a + track_b = notes on that
    pair plus each track's own notes (track_b NULL); track_a alone = every
    note touching track_a; neither = the latest notes."""
    from sqlalchemy import and_, or_
    from audiplex.models import DjPairNote

    q = db.query(DjPairNote)
    if track_a is not None and track_b is not None:
        q = q.filter(or_(
            and_(DjPairNote.track_a == track_a, DjPairNote.track_b == track_b),
            and_(DjPairNote.track_a.in_([track_a, track_b]), DjPairNote.track_b.is_(None)),
        ))
    elif track_a is not None:
        q = q.filter(or_(DjPairNote.track_a == track_a, DjPairNote.track_b == track_a))
    rows = q.order_by(DjPairNote.id.desc()).limit(limit).all()
    return [
        {"id": r.id, "track_a": r.track_a, "track_b": r.track_b, "note": r.note,
         "persona": r.persona, "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]


@router.post("/pair-notes", tags=["dj_library"])
def add_pair_note(body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """body: {track_a, track_b?, note, persona?} (#ride0928)."""
    from audiplex.models import DjPairNote, Track

    try:
        a = int(body["track_a"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="track_a (int) is required")
    b = body.get("track_b")
    b = int(b) if b not in (None, "", 0) else None
    note = str(body.get("note") or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="note is required")
    for tid in [a] + ([b] if b is not None else []):
        if db.get(Track, tid) is None:
            raise HTTPException(status_code=404, detail=f"no track {tid}")
    row = DjPairNote(track_a=a, track_b=b, note=note[:2000], persona=(body.get("persona") or None))
    db.add(row)
    db.commit()
    return {"id": row.id, "track_a": a, "track_b": b, "note": row.note, "persona": row.persona}


@router.post("/playlists", response_model=PlaylistSummary, tags=["dj_library"])
def create_owner_playlist(body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Create a playlist in the OWNER's library from track ids (#ride0928, dj_folder).

    /api/music/playlists writes to the caller, and dj-agent's playlists are
    invisible on Todd's phone. body: {name, track_ids}.
    """
    from audiplex.models import PlaylistTrack, Track

    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    owner = _resolve_owner(db)
    wanted = [int(i) for i in body.get("track_ids") or []]
    known = {r[0] for r in db.query(Track.id).filter(Track.id.in_(wanted)).all()} if wanted else set()
    pl = Playlist(name=name[:200], user_id=owner.id)
    db.add(pl)
    db.flush()
    pos = 0
    for tid in wanted:
        if tid in known:
            db.add(PlaylistTrack(playlist_id=pl.id, track_id=tid, position=pos))
            pos += 1
    db.commit()
    return PlaylistSummary(id=pl.id, name=pl.name, track_count=pos)
