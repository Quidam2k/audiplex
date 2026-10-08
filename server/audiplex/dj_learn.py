"""The DJ learns from every ride (#4057).

After a ride closes, Todd's own skips and play-throughs nudge a per-track pick
weight that dj_pool reads on its next top-up. Nothing changes mid-ride.

Only TODD's skips count. A DJ skip ("skip"/"play_now"/... bus command) makes
the phone post the same 'skip' play-stat a button press does, so a skip within
ATTRIB_BEFORE_S after (or ATTRIB_AFTER_S before) an agent advance command in the
playback diag log is the DJ's and is ignored. A voice skip Todd asked for is
relayed by an agent with payload by='todd' and counts as his.

Weight 0 is a soft drop, named in the ride note; a 4+ star rating restores it.
"""

from __future__ import annotations

import json
import random
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from audiplex.models import DjLearnRun, DjTrackWeight, PlayStat, Track, TrackRating, _utcnow

FAST_SKIP_S = 5.0
ATTRIB_BEFORE_S = 15.0
ATTRIB_AFTER_S = 3.0
DROP_AFTER_RIDES = 2
LOVE_MIN_STARS = 4.0
ADVANCE_TYPES = frozenset({"skip", "play_now", "play_book", "play_stream"})
SKIP_FACTOR = 0.5
PLAYED_FACTOR = 1.1
LOVE_FACTOR = 1.3
MIN_W, MAX_W = 0.0, 2.0


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _naive_utc(value: datetime) -> datetime:
    return _as_utc(value).replace(tzinfo=None)


def agent_advance_times(since: datetime, until: datetime) -> list[float]:
    """Epoch times of agent track-advancing commands around [since, until]."""
    from audiplex import playback_bus

    lower = (_as_utc(since) - timedelta(seconds=ATTRIB_BEFORE_S)).timestamp()
    upper = (_as_utc(until) + timedelta(seconds=ATTRIB_AFTER_S)).timestamp()
    path = Path(playback_bus.DIAG_LOG_PATH)
    paths = [path, *(path.with_name(f"{path.name}.{n}") for n in range(1, playback_bus.DIAG_LOG_BACKUPS))]
    advances: list[float] = []
    for log_path in paths:
        try:
            with log_path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        cmd = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(cmd, dict) or cmd.get("kind") != "cmd_queued":
                        continue
                    payload = cmd.get("payload")
                    if cmd.get("type") not in ADVANCE_TYPES or (isinstance(payload, dict) and payload.get("by") == "todd"):
                        continue
                    at = cmd.get("at")
                    if isinstance(at, (int, float)) and lower <= at <= upper:
                        advances.append(float(at))
        except OSError:
            continue
    return sorted(advances)


def _names(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "nothing"
    names = [f"{e['title']} - {e['artist']}" for e in entries[:5]]
    if len(entries) > 5:
        names.append(f"+{len(entries) - 5} more")
    return ", ".join(names)


def learn(
    db: Session,
    owner_id: int,
    since: datetime,
    until: datetime,
    ride_id: str,
    advance_times: list[float] | None = None,
) -> dict[str, Any]:
    """Apply one ride's lessons. Replaying a ride_id returns its first summary."""
    existing = db.get(DjLearnRun, ride_id)
    if existing is not None:
        return {**json.loads(existing.summary), "already_learned": True}

    lo, hi = _naive_utc(since), _naive_utc(until)
    advances = sorted(agent_advance_times(since, until) if advance_times is None else advance_times)

    todd_skips = agent_skips = 0
    todd_skipped: set[int] = set()
    fast_skipped: set[int] = set()
    completed: set[int] = set()
    stats = db.scalars(select(PlayStat).where(
        PlayStat.user_id == owner_id, PlayStat.timestamp >= lo, PlayStat.timestamp <= hi))
    for stat in stats:
        if stat.event == "complete":
            completed.add(stat.track_id)
        elif stat.event == "skip":
            ts = _as_utc(stat.timestamp).timestamp()
            i = bisect_left(advances, ts - ATTRIB_BEFORE_S)
            if i < len(advances) and advances[i] <= ts + ATTRIB_AFTER_S:
                agent_skips += 1
                continue
            todd_skips += 1
            todd_skipped.add(stat.track_id)
            if stat.played_seconds < FAST_SKIP_S:
                fast_skipped.add(stat.track_id)

    played_through = completed - todd_skipped
    loved = set(db.scalars(select(TrackRating.track_id).where(
        TrackRating.user_id == owner_id, TrackRating.rating >= LOVE_MIN_STARS,
        TrackRating.updated_at >= lo, TrackRating.updated_at <= hi)))

    down: list[dict[str, Any]] = []
    up: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    now = _utcnow()
    for tid in sorted(fast_skipped | played_through | loved):
        row = db.get(DjTrackWeight, tid)
        before = row.weight if row is not None else 1.0
        if not (tid in fast_skipped or tid in loved or before > 0):
            continue  # a dropped track that somehow played through stays dropped
        if row is None:
            row = DjTrackWeight(track_id=tid, weight=1.0, skip_rides=0)
        weight = before
        if tid in fast_skipped:
            weight *= SKIP_FACTOR
            row.skip_rides += 1
            row.reason = f"fast-skipped by you (<{FAST_SKIP_S:g} s)"
            if row.skip_rides >= DROP_AFTER_RIDES:
                weight = 0.0
                row.reason = f"dropped: you skipped it on {row.skip_rides} rides"
        if tid in played_through and weight > 0:
            weight *= PLAYED_FACTOR
        if tid in loved:
            if weight == 0:
                weight, row.skip_rides = 1.0, 0
            weight *= LOVE_FACTOR
            row.reason = "loved"
        row.weight = round(max(MIN_W, min(MAX_W, weight)), 4)
        row.ride_id, row.updated_at = ride_id, now
        db.add(row)

        track = db.get(Track, tid)
        artist = getattr(track, "artist", None)
        entry = {
            "track_id": tid,
            "title": (track.title if track is not None and track.title else f"Track {tid}"),
            "artist": getattr(artist, "name", None) or "Unknown artist",
            "weight": row.weight,
        }
        if row.weight == 0:
            dropped.append(entry)
        elif row.weight < before:
            down.append(entry)
        elif row.weight > before:
            up.append(entry)

    summary: dict[str, Any] = {
        "ride_id": ride_id,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "todd_skips": todd_skips,
        "agent_skips_ignored": agent_skips,
        "down": down,
        "up": up,
        "dropped": dropped,
        "note": "\n".join((
            f"Ride {ride_id}: {todd_skips} skip(s) by you, {agent_skips} by the DJ ignored.",
            f"Down: {_names(down)}. Up: {_names(up)}.",
            (f"Dropped from the pool: {_names(dropped)}. Rate it 4+ stars or love it to bring it back."
             if dropped else "Nothing dropped."),
        )),
    }
    db.add(DjLearnRun(ride_id=ride_id, since=lo, until=hi, summary=json.dumps(summary)))
    db.commit()
    return {**summary, "already_learned": False}


def weights_for(db: Session) -> dict[int, float]:
    return dict(db.execute(select(DjTrackWeight.track_id, DjTrackWeight.weight)).all())


def restore(db: Session, track_id: int) -> bool:
    """Bring a dropped track back to neutral. False when it was not dropped."""
    row = db.get(DjTrackWeight, track_id)
    if row is None or row.weight != 0:
        return False
    row.weight, row.skip_rides, row.reason, row.updated_at = 1.0, 0, "restored by your rating", _utcnow()
    db.commit()
    return True


def weighted_pick(ids: list[int], weights: dict[int, float], rng=random) -> int:
    """Pick one id, weight-proportional (unknown ids weigh 1.0). Callers drop weight-0 ids first."""
    w = [weights.get(t, 1.0) for t in ids]
    return rng.choices(ids, weights=w, k=1)[0] if sum(w) > 0 else rng.choice(ids)
