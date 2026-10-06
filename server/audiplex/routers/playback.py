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
  GET  /api/playback/diag-history  — client-log, command and state changes, persisted (#3249)
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
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, selectinload

from audiplex import taste
from audiplex.identity import build_identity_map
from audiplex.dj_bans import banned_ids  # #2806
from audiplex.mix import plan_mix, plan_summary
from audiplex.scheduled_stop import controller as stop_controller  # #3505
from audiplex.auth import get_current_user
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import Favorite, Playlist, TrackRating, User
from audiplex.playback_bus import (
    LEGACY_DEVICE_ID,
    bus,
    read_diag_history,
    read_link_history,
    read_persisted_exits,
)
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
    VerbalRatingRequest,  # #3576
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
    # #3505: a stop Todd asked for holds until an explicit start lifts it.
    refusal = stop_controller.gate(cmd.type, cmd.payload or {}, time.time(), bus)
    if refusal:
        raise HTTPException(status_code=409, detail=refusal)
    # #3552: record who sent it; the MCP adds "<persona>:<tool>".
    source = user.username + (f"/{cmd.source}" if cmd.source else "")
    rec = await bus.enqueue(cmd.type, cmd.payload, source=source)
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
    rec = bus.ack(command_id, ack.status, ack.detail or "")
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


@router.get("/resume")
def get_resume(
    device_id: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    """The last real music queue a renderer reported, persisted (#3601).

    The queue otherwise lives only in the phone's RAM: after a pause plus an
    app kill it was gone, and a 1,300-track DJ mix can't be rebuilt by hand.
    dj_resume and the app's own restore read this. 404 when nothing saved.
    """
    from audiplex.playback_bus import read_last_queues

    snap = read_last_queues().get(device_id or LEGACY_DEVICE_ID)
    if not snap:
        raise HTTPException(status_code=404, detail="No saved queue for that device.")
    return {**snap, "age_seconds": round(time.time() - float(snap.get("at") or 0), 1)}


@router.get("/state")
def get_state(
    device_id: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    """Now-playing of `device_id`, or by default of whichever device is rendering.

    #3505: `stop` carries any armed/finished DJ stop and the stop latch, so a
    persona reading now-playing sees WHY queueing is refused."""
    stop = stop_controller.public(time.time(), bus)
    state = bus.get_state(device_id)
    if state is not None:
        return {**state, "stop": stop}
    return {
        "playing": False,
        "track": None,
        "position_ms": 0,
        "duration_ms": 0,
        "queue_length": 0,
        "queue_index": 0,
        "queue": [],
        "volume": None,
        "updated_at": None,
        "stop": stop,
    }


# ----- #3505: verified stops -----


@router.post("/scheduled-stop")
def arm_scheduled_stop(body: dict, user: User = Depends(get_current_user)):
    """Arm a server-run, device-verified stop.

    body {"mode": "after_current"}: trim the queue tail, stop the DJ pool,
    latch against refills, and make sure the device stops at the end of the
    current song (pause ~1 s before its end if the trim isn't acked ok).
    body {"mode": "fade", "minutes": m, "fade_seconds": f}: after m minutes
    ramp the volume to 0 over the last f seconds, pause, restore the volume.
    Poll GET for the verdict: YES only once the device reports playing=no.
    """
    mode = body.get("mode")
    now = time.time()
    if mode == "after_current":
        result = stop_controller.arm_after_current(bus, now)
    elif mode == "fade":
        try:
            minutes = float(body.get("minutes"))
            fade = float(body.get("fade_seconds", 120))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="fade needs numeric minutes (and fade_seconds)")
        result = stop_controller.arm_fade(bus, now, minutes, fade)
    else:
        raise HTTPException(status_code=422, detail="mode must be 'after_current' or 'fade'")
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return result


@router.get("/scheduled-stop")
def get_scheduled_stop(user: User = Depends(get_current_user)):
    return stop_controller.public(time.time(), bus)


@router.delete("/scheduled-stop")
def cancel_scheduled_stop(user: User = Depends(get_current_user)):
    """Cancel any armed stop and lift the stop latch."""
    had = stop_controller.clear("cancelled by DELETE", bus, time.time())
    return {"cleared": had, **stop_controller.public(time.time(), bus)}


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


def stored_stars(stars: float) -> float:
    """What's stored for a spoken rating: exact halves since #6117 (it was the
    floor while the app could only show whole stars, #3576)."""
    return max(0.5, min(5.0, float(stars)))


def verbal_note(stars: float, words: str, persona: str) -> str:
    """'[Juno, said 4.5] one of the all-time greats' — who relayed it, the exact
    number he said, and his own words, which carry the most signal."""
    head = f"[{persona.strip() or 'DJ'}, said {stars:g}]"
    words = " ".join(words.split())
    return (f"{head} {words}" if words else head)[:500]


@router.put("/ratings")
def set_owner_ratings(
    body: VerbalRatingRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Set the OWNER's star rating on tracks — the same track_ratings row the
    app's star tap writes (#3576).

    Owner-scoped like the reads above: personas authenticate as dj-agent, and
    /api/music/tracks/{id}/rating would rate as dj-agent, which Todd never
    sees. One short transaction for every id (busy_timeout covers a scan lock).
    """
    if body.stars * 2 != int(body.stars * 2):
        raise HTTPException(status_code=422, detail="Stars go in halves: 1, 1.5 ... 5.")
    from datetime import datetime, timezone

    from audiplex.models import Track

    owner = _resolve_owner(db)
    ids = sorted({int(i) for i in body.track_ids})
    known = {r[0] for r in db.query(Track.id).filter(Track.id.in_(ids)).all()}
    stored = stored_stars(body.stars)
    note = verbal_note(body.stars, body.words, body.persona)
    existing = {
        r.track_id: r
        for r in db.query(TrackRating).filter(
            TrackRating.user_id == owner.id, TrackRating.track_id.in_(known)
        )
    }
    now = datetime.now(timezone.utc)
    rated = []
    for tid in sorted(known):
        row = existing.get(tid)
        was = row.rating if row else None
        if row:
            row.rating, row.note, row.updated_at = stored, note, now
        else:
            db.add(TrackRating(user_id=owner.id, track_id=tid, rating=stored, note=note))
        rated.append({"track_id": tid, "rating": stored, "was": was})
    db.commit()
    return {
        "stars": body.stars,
        "stored": stored,
        "note": note,
        "rated": rated,
        "unknown": [i for i in ids if i not in known],
    }


@router.delete("/ratings")
def clear_owner_ratings(
    body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """body: {track_ids}. Clear the owner's stars — "never mind, take that back"."""
    ids = {int(i) for i in body.get("track_ids") or []}
    if not ids:
        return {"deleted": 0}
    owner = _resolve_owner(db)
    deleted = (
        db.query(TrackRating)
        .filter(TrackRating.user_id == owner.id, TrackRating.track_id.in_(ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": deleted}


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
    long_ids = too_long_for_mix(db, body.new_ids)  # #3249
    skip |= long_ids
    skip |= banned_ids(db, identities)  # #2806
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
        skipped_long=sorted(long_ids),
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

    # #6913: a pool would refill a queue Todd asked to end.
    if stop_controller.latch_active(time.time()):
        raise HTTPException(status_code=409, detail=(
            f"STOPPED: {stop_controller.latch['reason']}. The pool was not started. "
            "If Todd asks for music again, dj_play_now lifts the stop (or dj_stop_cancel)."))
    pool = get_pool()
    spec_id = body.get("spec_id")
    balance = body.get("balance", "even")
    ahead = int(body.get("ahead", 4))
    refill_at = body.get("refill_at")  # #3644
    refill_at = int(refill_at) if refill_at is not None else None
    exclude_recent_hours = body.get("exclude_recent_hours", 12)
    raw_lanes = body.get("lanes")
    if raw_lanes:
        lanes = {str(label): [int(t) for t in ids] for label, ids in raw_lanes.items()}
        speech = non_music(db, [t for ids in lanes.values() for t in ids])  # #ride0928
        long_ids: set[int] = set()  # #3249: per lane, so a one-track lane is kept
        for ids in lanes.values():
            long_ids |= too_long_for_mix(db, ids)
        banned = banned_ids(db)  # #2806
        dropped = {  # #7108: so a lane left empty can say why
            label: {"non-music": sum(t in speech for t in ids),
                    "too long for a mix": sum(t in long_ids and t not in speech for t in ids),
                    "banned": sum(t in banned and t not in speech | long_ids for t in ids)}
            for label, ids in lanes.items()
        }
        skip = speech | long_ids | banned
        lanes = {label: [t for t in ids if t not in skip] for label, ids in lanes.items()}
        track_ids = [t for ids in lanes.values() for t in ids]
        source_labels = {t: label for label, ids in lanes.items() for t in ids}
        no_repeat = body.get("no_repeat_picks")  # #7108
        result = pool.set_pool(
            spec_id, track_ids, source_labels, lanes=lanes_from_ids(lanes, dropped),
            balance=balance, ahead=ahead, exclude_recent_hours=exclude_recent_hours,
            refill_at=refill_at, no_repeat_picks=int(no_repeat) if no_repeat is not None else None,
        )
    else:
        legacy_ids = [int(t) for t in body.get("track_ids", [])]
        long_ids = too_long_for_mix(db, legacy_ids)  # #3249
        drop = long_ids | banned_ids(db)  # #2806
        result = pool.set_pool(
            spec_id, [t for t in legacy_ids if t not in drop],  # #2806
            body.get("source_labels", {}),
            balance=balance, ahead=ahead, exclude_recent_hours=exclude_recent_hours,
            refill_at=refill_at,
        )
    result["skipped_long"] = sorted(long_ids)  # #3249
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
        result["per_lane_details"] = top.get("per_lane_details", [])  # #7108
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


@router.patch("/pool/lanes", tags=["dj_pool"])
def set_pool_lane(body: dict, user: User = Depends(get_current_user)):
    """Pause, resume or remove one lane of the running pool (#2806).

    body: {lane: <label, case-insensitive prefix ok>, action: pause|resume|remove}.
    #3644: a pause or remove also re-picks what is queued after the current
    song, since the pool now keeps a deep queue (a resume shows at the next
    refill).
    """
    from audiplex.dj_pool import get_pool

    action = str(body.get("action", ""))
    try:
        result = get_pool().set_lane(str(body.get("lane", "")), action)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e.args[0] if e.args else e))
    if action in ("pause", "remove"):
        result["queue"] = bus.replan_pool(f"{user.username}/pool_lane:{action}")
    return result


# ----- #2806: DJ bans: never pick this track again (reversible) -----


@router.get("/bans", tags=["dj_library"])
def list_bans(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from audiplex.models import DjBan, Track

    rows = db.query(DjBan, Track).outerjoin(Track, Track.id == DjBan.track_id).order_by(DjBan.created_at).all()
    return [
        {"track_id": b.track_id, "title": t.title if t else None,
         "artist": t.artist.name if t and t.artist else None,
         "reason": b.reason, "persona": b.persona, "created_at": b.created_at.isoformat()}
        for b, t in rows
    ]


@router.post("/bans", tags=["dj_library"])
def add_bans(body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """body: {track_ids: [...], reason?, persona?}. Positive catalog ids only."""
    from audiplex.models import DjBan, Track

    ids = {int(i) for i in body.get("track_ids") or [] if int(i) > 0}
    known = {r[0] for r in db.query(Track.id).filter(Track.id.in_(ids)).all()} if ids else set()
    added = []
    for tid in sorted(known):
        if db.get(DjBan, tid) is None:
            db.add(DjBan(track_id=tid, reason=body.get("reason"), persona=body.get("persona")))
            added.append(tid)
    db.commit()
    return {"banned": added, "already": sorted(known - set(added)), "unknown": sorted(ids - known)}


@router.delete("/bans", tags=["dj_library"])
def remove_bans(body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """body: {track_ids: [...]}. Lifts the ban; the track can be picked again."""
    from audiplex.models import DjBan

    ids = {int(i) for i in body.get("track_ids") or []}
    removed = [r.track_id for r in db.query(DjBan).filter(DjBan.track_id.in_(ids)).all()] if ids else []
    if removed:
        db.query(DjBan).filter(DjBan.track_id.in_(removed)).delete(synchronize_session=False)
        db.commit()
    return {"unbanned": sorted(removed), "not_banned": sorted(ids - set(removed))}


# ----- #2806: DJ tags (applied, never inferred) and measured-energy arcs -----


def _norm_tags(tags) -> list[str]:
    out = []
    for t in tags or []:
        t = " ".join(str(t).lower().split())[:40]
        if t and t not in out:
            out.append(t)
    return out


@router.get("/tags", tags=["dj_library"])
def list_tags(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Every DJ tag with how many tracks carry it."""
    from sqlalchemy import func

    from audiplex.models import DjTrackTag

    rows = db.query(DjTrackTag.tag, func.count()).group_by(DjTrackTag.tag).order_by(DjTrackTag.tag).all()
    return [{"tag": t, "count": n} for t, n in rows]


@router.get("/tags/{tag}", tags=["dj_library"])
def tagged_tracks(tag: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The tracks carrying one tag, with their measured energy (None = unmeasured)."""
    from audiplex.models import DjTrackTag, Track

    norm = _norm_tags([tag])
    rows = (db.query(Track).join(DjTrackTag, DjTrackTag.track_id == Track.id)
            .filter(DjTrackTag.tag == (norm[0] if norm else "")).order_by(Track.id).all())
    return [{"track_id": t.id, "title": t.title, "artist": t.artist.name if t.artist else None,
             "energy": t.energy} for t in rows]


@router.post("/tags", tags=["dj_library"])
def add_tags(body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """body: {track_ids, tags, persona?}. Idempotent; unknown ids are reported."""
    from audiplex.models import DjTrackTag, Track

    tags = _norm_tags(body.get("tags"))
    if not tags:
        raise HTTPException(status_code=400, detail="Give at least one tag.")
    ids = {int(i) for i in body.get("track_ids") or [] if int(i) > 0}
    known = {r[0] for r in db.query(Track.id).filter(Track.id.in_(ids)).all()} if ids else set()
    have = {(r.track_id, r.tag) for r in db.query(DjTrackTag).filter(DjTrackTag.track_id.in_(known)).all()} if known else set()
    added = 0
    for tid in sorted(known):
        for tag in tags:
            if (tid, tag) not in have:
                db.add(DjTrackTag(track_id=tid, tag=tag, persona=body.get("persona")))
                added += 1
    db.commit()
    return {"tags": tags, "tracks": sorted(known), "added": added, "unknown": sorted(ids - known)}


@router.delete("/tags", tags=["dj_library"])
def remove_tags(body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """body: {track_ids, tags?}. No tags = every tag on those tracks."""
    from audiplex.models import DjTrackTag

    ids = {int(i) for i in body.get("track_ids") or []}
    if not ids:
        return {"removed": 0}
    q = db.query(DjTrackTag).filter(DjTrackTag.track_id.in_(ids))
    tags = _norm_tags(body.get("tags"))
    if tags:
        q = q.filter(DjTrackTag.tag.in_(tags))
    removed = q.delete(synchronize_session=False)
    db.commit()
    return {"removed": removed}


@router.post("/energy/arc", tags=["dj_library"])
def energy_arc(body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Order candidates into an energy arc (#2806). Pure planning: queues nothing.

    body: {track_ids, arc: rise|peak|wind_down|steady, minutes?, tags?: all
    required, min_energy?, max_energy?, seed?}. Tracks without a measured
    energy are left out and counted; banned, too-long and non-music are dropped.
    """
    from audiplex.energy_arc import order_arc
    from audiplex.models import DjTrackTag, Track

    ids = list(dict.fromkeys(int(i) for i in body.get("track_ids") or []))
    drop = non_music(db, ids) | too_long_for_mix(db, ids) | banned_ids(db)
    ids = [i for i in ids if i not in drop]
    tags = _norm_tags(body.get("tags"))
    untagged = 0
    if tags and ids:
        rows = db.query(DjTrackTag.track_id, DjTrackTag.tag).filter(
            DjTrackTag.track_id.in_(ids), DjTrackTag.tag.in_(tags)).all()
        has: dict[int, set] = {}
        for tid, tag in rows:
            has.setdefault(tid, set()).add(tag)
        keep = [i for i in ids if has.get(i, set()) >= set(tags)]
        untagged, ids = len(ids) - len(keep), keep
    lo, hi = body.get("min_energy"), body.get("max_energy")
    rows = db.query(Track.id, Track.energy, Track.duration_seconds).filter(Track.id.in_(ids)).all() if ids else []
    measured = [(t, e, d) for t, e, d in rows if e is not None
                and (lo is None or e >= int(lo)) and (hi is None or e <= int(hi))]
    try:
        ordered = order_arc(measured, str(body.get("arc", "")), float(body.get("minutes") or 0), body.get("seed"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    by_id = {t: (e, d) for t, e, d in measured}
    return {
        "ordered": ordered,
        "energies": [by_id[t][0] for t in ordered],
        "minutes": round(sum(by_id[t][1] or 0 for t in ordered) / 60, 1),
        "unmeasured": sum(1 for _, e, _ in rows if e is None),
        "out_of_range": sum(1 for _, e, _ in rows if e is not None) - len(measured),
        "untagged": untagged,
        "dropped": len(drop),
    }


@router.post("/harmonic/order", tags=["dj_library"])
def harmonic_order(body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Order candidates so each hand-off is key- and tempo-compatible (#1002). Queues nothing.

    body: {track_ids, minutes?, start_track_id?, bpm_tolerance? (0.06 = 6%),
    arc?: rise|peak|wind_down|steady, seed?}. Tracks without a measured tempo
    and key are left out and counted; banned, too-long and non-music are dropped.
    """
    from audiplex.harmonic import order_harmonic
    from audiplex.models import Track

    ids = list(dict.fromkeys(int(i) for i in body.get("track_ids") or []))
    drop = non_music(db, ids) | too_long_for_mix(db, ids) | banned_ids(db)
    ids = [i for i in ids if i not in drop]
    rows = db.query(Track.id, Track.bpm, Track.musical_key, Track.duration_seconds, Track.energy).filter(
        Track.id.in_(ids)).all() if ids else []
    analysed = [tuple(r) for r in rows if r[1] and r[2]]
    start = body.get("start_track_id")
    try:
        res = order_harmonic(
            analysed, float(body.get("minutes") or 0), int(start) if start else None,
            float(body.get("bpm_tolerance") or 0.06), body.get("arc") or None, body.get("seed"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    by_id = {r[0]: r for r in analysed}
    return {
        **res,
        "bpms": [by_id[t][1] for t in res["ordered"]],
        "keys": [by_id[t][2] for t in res["ordered"]],
        "minutes": round(sum(by_id[t][3] or 0 for t in res["ordered"]) / 60, 1),
        "unanalysed": len(rows) - len(analysed),
        "dropped": len(drop),
    }


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
        bus.replan_pool(f"{user.username}/spec_edit")  # #3644: show the edit now

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


# #3249: 2026-09-28 21:49 a 62-minute soundtrack file ("Mr. Robot OST Vol 4")
# landed in the ride mix and Todd skipped it at 9 minutes. A MUSIC mix or pool
# drops anything longer than this. It never applies to what a mix doesn't touch
# (play_now of a named track, audiobooks), and a source that resolves to ONE
# track is kept, since that one was asked for by name.
MAX_MIX_TRACK_SECONDS = 20 * 60
_long_skip_logged: set[int] = set()


def too_long_for_mix(db: Session, track_ids) -> set[int]:  # #3249
    """Music ids in a multi-track source whose duration is over the cap.
    Each skipped id is logged once per process so a long track that someone
    really wanted is visible, not silently gone."""
    from audiplex.models import Track
    from audiplex.playback_bus import _append_diag

    ids = {int(i) for i in track_ids if int(i) > 0}
    if len(ids) <= 1:
        return set()
    rows = (
        db.query(Track.id, Track.title, Track.duration_seconds)
        .filter(
            Track.id.in_(ids),
            Track.content_kind == "music",
            Track.duration_seconds > MAX_MIX_TRACK_SECONDS,
        )
        .all()
    )
    for tid, title, secs in rows:
        if tid not in _long_skip_logged:
            _long_skip_logged.add(tid)
            print(f"[mix] #3249 skipped over-{MAX_MIX_TRACK_SECONDS // 60}-min track "
                  f"{tid} {title!r} ({secs / 60:.0f} min)", flush=True)
            _append_diag("mix_skip_long", {"track_id": tid, "title": title,
                                           "duration_seconds": secs})
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

    def epoch(ts):
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()

    # #3255: pair each start with the first stop/complete/skip of the same
    # track after it (and before that track starts again), so a caller can
    # join on "what was playing between start and end".
    ends_by_track: dict[int, list] = {}
    starts_by_track: dict[int, list] = {}
    start_rows = [ps for ps, _ in rows if ps.event == "start"]
    if start_rows:
        track_ids = {ps.track_id for ps in start_rows}
        earliest = min(ps.timestamp for ps in start_rows)
        for ps in (
            db.query(PlayStat)
            .filter(
                PlayStat.user_id == owner.id,
                PlayStat.track_id.in_(track_ids),
                PlayStat.timestamp >= earliest,
            )
            .order_by(PlayStat.timestamp, PlayStat.id)
        ):
            bucket = starts_by_track if ps.event == "start" else ends_by_track
            bucket.setdefault(ps.track_id, []).append(ps)

    def end_of(start):
        if start.event != "start":
            return None
        next_start = next(
            (s for s in starts_by_track.get(start.track_id, [])
             if (s.timestamp, s.id) > (start.timestamp, start.id)),
            None,
        )
        for e in ends_by_track.get(start.track_id, []):
            if (e.timestamp, e.id) < (start.timestamp, start.id):
                continue
            if next_start is not None and (e.timestamp, e.id) > (next_start.timestamp, next_start.id):
                break
            return e
        return None

    out = []
    for ps, t in rows:
        at = epoch(ps.timestamp)
        end = end_of(ps)
        out.append({
            "track_id": t.id,
            "id": t.id,
            "title": t.title,
            "artist_name": t.artist.name if t.artist else None,
            "event": ps.event,
            "played_seconds": ps.played_seconds,
            "at": at,
            "start": at,
            "end": epoch(end.timestamp) if end is not None else None,
            "end_event": end.event if end is not None else None,
            "loudness_lufs": t.loudness_lufs,
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


@router.get("/diag-history")
def get_diag_history(
    limit: int = Query(200, ge=1, le=2000),
    kind: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    """Client-log entries, command queued/delivered/ack, and now-playing
    changes, from disk (#3249). The live views are memory-only, and a restart
    on 2026-09-27 and 09-28 wiped the evidence for why two rides halted ~6 s
    before the last song ended. `kind` filters to one of client_log,
    cmd_queued, cmd_delivered, cmd_ack, state.
    """
    return read_diag_history(limit, kind)
