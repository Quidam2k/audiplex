"""Repeat and five-star spacing guard for agent queue commands (#7335)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

GUARDED_OPS = {"queue", "play_next", "replace_upcoming", "play_now"}
WORK_COOLDOWN_SECONDS = 24 * 3600
STOP_MIN_PLAYED_SECONDS = 30.0
COUNTED_EVENTS = {"complete", "skip"}
FIVE_STAR = 5.0
SPACING = 4


def is_agent_source(source: str | None) -> bool:
    """Identify agent and automation sources; bare usernames are unguarded."""
    return bool(
        source
        and (
            "/" in source
            or ":" in source
            or source.startswith(("dj", "ride_fallback", "scheduled_stop"))
        )
    )


@dataclass(frozen=True)
class Play:
    """A playback event at a Unix timestamp."""

    track_id: int
    at: float
    event: str
    played_seconds: float


@dataclass
class GuardInput:
    """Queue context and owner data needed by the pure guard."""

    op: str
    incoming: list[int]
    current_id: int | None
    upcoming: list[int]
    reserved: list[int]
    plays: list[Play]
    ratings: dict[int, float]
    work: dict[int, str]
    titles: dict[int, str]
    now: float


@dataclass
class GuardResult:
    """Accepted order, explanatory notes, dropped ids, and deferred ids."""

    kept: list[int]
    notes: list[str]
    dropped: list[int]
    moved: list[int]


def _counts(play: Play) -> bool:
    """Exclude starts and stops shorter than the counting threshold."""
    return play.event in COUNTED_EVENTS or (
        play.event == "stop" and play.played_seconds >= STOP_MIN_PLAYED_SECONDS
    )


def apply_guard(inp: GuardInput) -> GuardResult:
    """Deduplicate works, apply cooldown, and space five-stars without I/O."""
    if inp.op not in GUARDED_OPS or (
        inp.op == "play_now" and len(inp.incoming) <= 1
    ):
        return GuardResult(list(inp.incoming), [], [], [])

    def key(tid: int) -> str:
        return inp.work.get(tid, f"id:{tid}")

    def title(tid: int) -> str:
        return inp.titles.get(tid, f"track {tid}")

    def five(tid: int) -> bool:
        return inp.ratings.get(tid, 0) >= FIVE_STAR

    current = [inp.current_id] if inp.current_id is not None else []
    queued = [tid for tid in inp.upcoming + inp.reserved if tid is not None]
    if inp.op == "queue":
        fixed_before, fixed_after = current + queued, []
    elif inp.op == "play_next":
        fixed_before, fixed_after = current, queued
    elif inp.op == "replace_upcoming":
        fixed_before, fixed_after = current, []
    else:
        fixed_before, fixed_after = [], []

    blocked_by: dict[str, tuple[int, str]] = {}
    for tid in fixed_before + fixed_after:
        if tid <= 0:
            continue
        explanation = (
            "is playing now" if tid == inp.current_id else "is already queued"
        )
        blocked_by.setdefault(key(tid), (tid, explanation))

    latest: dict[str, Play] = {}
    for play in inp.plays:
        if not _counts(play) or inp.now - play.at >= WORK_COOLDOWN_SECONDS:
            continue
        wk = key(play.track_id)
        previous = latest.get(wk)
        if previous is None or play.at > previous.at:
            latest[wk] = play

    candidates: list[int] = []
    notes: list[str] = []
    dropped_ids: list[int] = []
    for tid in inp.incoming:
        if tid <= 0:
            candidates.append(tid)
            continue
        wk = key(tid)
        holder = blocked_by.get(wk)
        if holder is not None:
            holder_id, explanation = holder
            notes.append(
                f"dropped {title(tid)}: same song as "
                f"{title(holder_id)} {explanation}"
            )
            dropped_ids.append(tid)
        elif wk in latest:
            play = latest[wk]
            played_at = time.strftime("%H:%M", time.localtime(play.at))
            notes.append(
                f"dropped {title(tid)}: same song as "
                f"{title(play.track_id)} played {played_at}"
            )
            dropped_ids.append(tid)
        else:
            candidates.append(tid)
            blocked_by[wk] = (tid, "is already in this batch")

    n = len(candidates)
    base = len(fixed_before)
    fixed_five_positions = [
        i for i, tid in enumerate(fixed_before) if five(tid)
    ] + [
        base + n + j for j, tid in enumerate(fixed_after) if five(tid)
    ]
    fives = [tid for tid in candidates if five(tid)]
    fillers = [tid for tid in candidates if not five(tid)]
    five_count = len(fives)
    placed_five_positions: list[int] = []

    def fits(pos: int) -> bool:
        return all(
            abs(p - pos) >= SPACING
            for p in fixed_five_positions + placed_five_positions
        )

    result: list[int] = []
    deferred: list[int] = []
    while len(result) < n:
        pos = base + len(result)
        if fives and fits(pos):
            result.append(fives.pop(0))
            placed_five_positions.append(pos)
        elif fillers:
            result.append(fillers.pop(0))
        elif fives:
            deferred = fives
            fives = []
            break
        else:
            break

    for tid in deferred:
        pos = base + len(result)
        result.append(tid)
        placed_five_positions.append(pos)
        notes.append(
            f"moved {title(tid)} to the end of the batch: "
            f"not enough other tracks to keep five-stars {SPACING} apart"
        )
    result.extend(fillers)
    if result != candidates and five_count >= 2:
        notes.append(f"spaced {five_count} five-star tracks apart from each other")

    return GuardResult(
        kept=result, notes=notes, dropped=dropped_ids, moved=list(deferred)
    )


def load_guard_input(
    db: Session,
    *,
    op: str,
    incoming: list[int],
    current_id: int | None,
    upcoming: list[int],
    reserved: list[int],
    owner_id: int | None,
    now: float,
) -> GuardInput:
    """Load recent counted plays, work identities, titles, and owner ratings."""
    inp = GuardInput(
        op=op,
        incoming=list(incoming),
        current_id=current_id,
        upcoming=list(upcoming),
        reserved=list(reserved),
        plays=[],
        ratings={},
        work={},
        titles={},
        now=now,
    )
    ids = set(inp.incoming + inp.upcoming + inp.reserved)
    if current_id is not None:
        ids.add(current_id)
    if not ids:
        return inp

    from audiplex.identity import work_key
    from audiplex.models import Artist, PlayStat, Track, TrackRating
    from audiplex.taste import _as_utc

    cutoff = datetime.fromtimestamp(now - WORK_COOLDOWN_SECONDS, timezone.utc)
    query = db.query(
        PlayStat.track_id, PlayStat.event, PlayStat.played_seconds, PlayStat.timestamp
    ).filter(PlayStat.timestamp >= cutoff)
    if owner_id is not None:
        query = query.filter(PlayStat.user_id == owner_id)
    for tid, event, played_seconds, timestamp in query.all():
        if tid <= 0:
            continue
        at = _as_utc(timestamp)
        if at is None:
            continue
        play = Play(tid, at.timestamp(), event, float(played_seconds or 0.0))
        if _counts(play):
            inp.plays.append(play)
            ids.add(tid)

    rows = (
        db.query(Track.id, Track.title, Artist.name)
        .join(Artist, Artist.id == Track.artist_id)
        .filter(Track.id.in_(ids))
        .all()
    )
    for tid, title, artist in rows:
        inp.work[tid] = work_key(title, artist)
        inp.titles[tid] = title or f"track {tid}"

    if owner_id is not None:
        rows = (
            db.query(TrackRating.track_id, TrackRating.rating)
            .filter(TrackRating.user_id == owner_id, TrackRating.track_id.in_(ids))
            .all()
        )
        inp.ratings = {tid: float(rating) for tid, rating in rows}
    return inp


def work_blocked_keys(
    work: dict[int, str],
    plays: list[Play],
    now: float,
    base: set[str] | None = None,
) -> set[str]:
    """Return a fresh set containing base keys and works under cooldown."""
    blocked = set(base) if base is not None else set()
    blocked.update(
        work.get(play.track_id, f"id:{play.track_id}")
        for play in plays
        if _counts(play) and now - play.at < WORK_COOLDOWN_SECONDS
    )
    return blocked
