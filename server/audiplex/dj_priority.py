"""DJ priority groups (#1006).

Todd's ask: "YouTube folder + Cirque heavier WITHOUT looping the same 12 songs".

Small, pure helpers used by dj_pool.top_up to favour priority groups
(boosted weights), keep recently played members cooled down, and cap how
much of the planned sequence any single group may occupy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable

from sqlalchemy import select

from audiplex.models import Artist, Track

DEFAULT_BOOST = 1.5
MIN_BOOST = 1.0
MAX_BOOST = 4.0

_SPLIT_RE = re.compile(r"[\\/]")


@dataclass(frozen=True)
class PriorityGroup:
    """A named set of matchers plus a weight multiplier."""

    name: str
    folder_segments: tuple[str, ...] = ()
    artists: tuple[str, ...] = ()
    boost: float = DEFAULT_BOOST


def _norm(value: object) -> str:
    return str(value).strip().casefold()


def _clamp_boost(value: object) -> float:
    try:
        boost = float(value)
    except (TypeError, ValueError):
        return DEFAULT_BOOST
    if boost != boost:  # NaN
        return DEFAULT_BOOST
    return max(MIN_BOOST, min(MAX_BOOST, boost))


def _names(value: object) -> tuple[str, ...]:
    """A matcher list from config; a bare string is one name, anything else non-list is none."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(s for s in (str(x).strip() for x in value) if s)


def groups_from_config(raw: list[dict]) -> list[PriorityGroup]:
    """Build groups from config dicts, skipping nameless or matcher-less items."""
    groups: list[PriorityGroup] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        segments = _names(item.get("folder_segments"))
        artists = _names(item.get("artists"))
        if not segments and not artists:
            continue
        boost = _clamp_boost(item.get("boost", DEFAULT_BOOST))
        groups.append(
            PriorityGroup(
                name=name,
                folder_segments=segments,
                artists=artists,
                boost=boost,
            )
        )
    return groups


def _parent_segments(file_path: str | None) -> set[str]:
    """Case-folded parent folder names of a path (filename dropped)."""
    if not file_path:
        return set()
    parts = _SPLIT_RE.split(file_path)[:-1]
    return {p.strip().casefold() for p in parts if p.strip()}


def group_members(db, groups: list[PriorityGroup]) -> dict[str, set[int]]:
    """Return {group name: member track ids}; {} when there are no groups."""
    if not groups:
        return {}
    stmt = (
        select(Track.id, Track.file_path, Artist.name)
        .outerjoin(Artist, Artist.id == Track.artist_id)
        .where((Track.content_kind.is_(None)) | (Track.content_kind == "music"))
    )
    prepared = [
        (
            g.name,
            {_norm(s) for s in g.folder_segments if _norm(s)},
            {_norm(a) for a in g.artists if _norm(a)},
        )
        for g in groups
    ]
    members: dict[str, set[int]] = {g.name: set() for g in groups}
    for track_id, file_path, artist_name in db.execute(stmt):
        segs = _parent_segments(file_path)
        artist = _norm(artist_name) if artist_name is not None else None
        for name, seg_set, artist_set in prepared:
            if (seg_set and segs & seg_set) or (
                artist is not None and artist in artist_set
            ):
                members[name].add(track_id)
    return members


def boosted_weights(
    weights: dict[int, float],
    members: dict[str, set[int]],
    groups: list[PriorityGroup],
    only: set[int] | None = None,
) -> dict[int, float]:
    """Return a new weights dict with group members boosted (largest boost wins)."""
    best: dict[int, float] = {}
    for g in groups:
        for tid in members.get(g.name, ()):
            if only is not None and tid not in only:
                continue
            if g.boost > best.get(tid, float("-inf")):
                best[tid] = g.boost
    new = dict(weights)
    for tid, boost in best.items():
        base = weights.get(tid, 1.0)
        if base <= 0:
            continue
        new[tid] = base * boost
    return new


def cooled(
    plays: Iterable,
    members: dict[str, set[int]],
    recording_of: Callable[[int], str | None],
    days: float,
    now: datetime,
) -> set[int]:
    """Member track ids played (same id or same recording) within `days` of now."""
    all_members: set[int] = set()
    for ids in members.values():
        all_members |= ids
    if not all_members:
        return set()
    cutoff = now - timedelta(days=days)
    recent_ids: set[int] = set()
    recent_recs: set[str] = set()
    for p in plays:
        at = p.at
        if at is None or at < cutoff or at > now:
            continue
        recent_ids.add(p.track_id)
        rec = getattr(p, "recording_id", None)
        if rec:
            recent_recs.add(rec)
    result: set[int] = set()
    for tid in all_members:
        if tid in recent_ids:
            result.add(tid)
            continue
        if recent_recs:
            rec = recording_of(tid)
            if rec and rec in recent_recs:
                result.add(tid)
    return result


def group_of(members: dict[str, set[int]]) -> dict[int, set[str]]:
    """Invert {group: track ids} into {track id: group names}."""
    out: dict[int, set[str]] = {}
    for name, ids in members.items():
        for tid in ids:
            out.setdefault(tid, set()).add(name)
    return out


def share_blocked(
    groups_by_track: dict[int, set[str]],
    sequence: list[int],
    window: int,
    max_share: float,
) -> set[str]:
    """Groups that would exceed max_share of `window` if one more were added."""
    if window <= 0 or max_share >= 1:
        return set()
    recent = sequence[-(window - 1):] if window > 1 else []
    names: set[str] = set().union(*groups_by_track.values()) if groups_by_track else set()
    counts = dict.fromkeys(names, 0)
    for tid in recent:
        for name in groups_by_track.get(tid, ()):
            counts[name] += 1
    limit = max_share * window
    return {name for name, n in counts.items() if n + 1 > limit}


def not_capped(
    candidates: list[int],
    groups_by_track: dict[int, set[str]],
    blocked: set[str],
) -> list[int]:
    """Candidates outside every blocked group; may be empty. The pool then lets the
    lane wait a turn, and plays it anyway only when no other lane can (like the
    artist rule), so the cap never stalls a set."""
    if not blocked:
        return candidates
    return [t for t in candidates if not (groups_by_track.get(t, set()) & blocked)]


def recent_sequence(plays, minutes: float, exclude: set[int]) -> list[int]:
    """Track ids started within `minutes`, oldest first, minus `exclude` (the queue,
    which the caller appends itself). plays: objects with .track_id, .event, .minutes_ago."""
    started = [p for p in plays if p.event == "start" and p.minutes_ago <= minutes
               and p.track_id not in exclude]
    started.sort(key=lambda p: p.minutes_ago, reverse=True)
    return [p.track_id for p in started]
