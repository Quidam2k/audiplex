"""Taste aggregation (#947) and recency cooldown (#948).

Both read the SAME event stream the client has been posting since long before
either feature existed: PlayStat is row-per-event (start / complete / skip /
stop) with the actual position at the discontinuity, not a bag of counters. So
nothing new is captured here. What was missing was aggregation and exposure:

  #947 — completion RATE, not raw completes. "Played eight times, finished
         twice" and "played twice, finished twice" are different opinions and
         raw counts hide the difference. Plus where in a track the abandons
         land, which is the difference between "wrong song" and "too long".

  #948 — a recent-play window. It needs no new storage at all; PlayStat has
         timestamps, so cooldown is a query.

One caveat that must not be lost: 'complete' is posted with the track's full
duration, not measured listening. It means REACHED THE END, not HEARD ALL OF
IT. Adequate for taste; not proof of attention, and nobody downstream should
later mistake it for that.

Everything aggregates by recording_id, never by track_id, so a local copy and
a streamed copy of one recording pool their history instead of each looking
half-listened-to. Cooldown additionally checks work_id. See identity.py.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from audiplex.identity import TrackIdentity, build_identity_map
from audiplex.models import PlayStat, TrackRating

# Abandoning a track in its first few seconds is the strongest dislike signal
# the client can produce. Canonical here; music.py re-exports it so the taste
# reads and the skip-suspect read cannot drift apart (#3028).
EARLY_SKIP_THRESHOLD_SECONDS = 10.0

ABANDON_EVENTS = ("skip", "stop")


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything written was UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RecordingStats:
    """One recording's listening history, pooled across every copy of it."""

    recording_id: str
    work_id: str
    track_ids: list[int]
    starts: int = 0
    completes: int = 0
    abandons: int = 0
    early_skips: int = 0
    completion_rate: float | None = None
    mean_skip_seconds: float | None = None
    median_skip_seconds: float | None = None
    last_played_at: datetime | None = None
    _abandon_positions: list[float] = field(default_factory=list, repr=False)

    def finalize(self) -> "RecordingStats":
        self.completion_rate = (
            round(self.completes / self.starts, 4) if self.starts else None
        )
        if self._abandon_positions:
            self.mean_skip_seconds = round(
                statistics.fmean(self._abandon_positions), 2
            )
            self.median_skip_seconds = round(
                statistics.median(self._abandon_positions), 2
            )
        return self


def recording_stats_for(
    db: Session, user_id: int, identities: dict[int, TrackIdentity] | None = None
) -> dict[str, RecordingStats]:
    """Aggregate one user's whole play history, keyed by recording_id.

    Aggregated in Python rather than SQL on purpose: a median has no portable
    SQLite expression, and grouping by a DERIVED identity would mean pushing
    the identity rules into the query. A few hundred tracks and a small event
    log make the trade free.
    """
    identities = identities if identities is not None else build_identity_map(db)

    stats: dict[str, RecordingStats] = {}
    for identity in identities.values():
        entry = stats.get(identity.recording_id)
        if entry is None:
            stats[identity.recording_id] = RecordingStats(
                recording_id=identity.recording_id,
                work_id=identity.work_id,
                track_ids=[identity.track_id],
            )
        else:
            entry.track_ids.append(identity.track_id)
    for entry in stats.values():
        entry.track_ids.sort()

    rows = (
        db.query(
            PlayStat.track_id, PlayStat.event, PlayStat.played_seconds, PlayStat.timestamp
        )
        .filter(PlayStat.user_id == user_id)
        .all()
    )
    for track_id, event, played_seconds, timestamp in rows:
        identity = identities.get(track_id)
        if identity is None:  # event for a track that has since left the library
            continue
        entry = stats[identity.recording_id]
        position = float(played_seconds or 0.0)
        at = _as_utc(timestamp)
        if at and (entry.last_played_at is None or at > entry.last_played_at):
            entry.last_played_at = at
        if event == "start":
            entry.starts += 1
        elif event == "complete":
            entry.completes += 1
        elif event in ABANDON_EVENTS:
            entry.abandons += 1
            entry._abandon_positions.append(position)
            if position < EARLY_SKIP_THRESHOLD_SECONDS:
                entry.early_skips += 1

    return {key: entry.finalize() for key, entry in stats.items()}


def ranked_recording_stats(
    db: Session, user_id: int, limit: int, min_starts: int = 1
) -> list[RecordingStats]:
    """Recordings with real history, most-listened first.

    Recordings with fewer than `min_starts` starts are dropped rather than
    ranked: a completion rate computed from one play is noise wearing the
    costume of a number.
    """
    everything = recording_stats_for(db, user_id).values()
    interesting = [
        entry
        for entry in everything
        if entry.starts >= min_starts or entry.completes or entry.abandons
    ]
    interesting.sort(
        key=lambda e: (e.completes, e.starts, e.last_played_at or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    return interesting[:limit]


@dataclass
class RecentPlay:
    track_id: int
    recording_id: str
    work_id: str
    event: str
    at: datetime
    minutes_ago: float


def recent_plays_for(
    db: Session,
    user_id: int,
    window_minutes: float,
    identities: dict[int, TrackIdentity] | None = None,
) -> list[RecentPlay]:
    """Everything that hit the deck inside the window, newest first.

    Any event counts, including an early skip. A track abandoned two minutes
    ago is still a track Todd just heard the front of — replaying it is the
    annoyance #948 is about, and it is also the one he liked least.
    """
    identities = identities if identities is not None else build_identity_map(db)
    now = _now()
    cutoff = now - timedelta(minutes=window_minutes)

    rows = (
        db.query(PlayStat.track_id, PlayStat.event, PlayStat.timestamp)
        .filter(PlayStat.user_id == user_id)
        .all()
    )
    plays: list[RecentPlay] = []
    for track_id, event, timestamp in rows:
        at = _as_utc(timestamp)
        identity = identities.get(track_id)
        if at is None or at < cutoff or identity is None:
            continue
        plays.append(
            RecentPlay(
                track_id=track_id,
                recording_id=identity.recording_id,
                work_id=identity.work_id,
                event=event,
                at=at,
                minutes_ago=round((now - at).total_seconds() / 60.0, 2),
            )
        )
    plays.sort(key=lambda p: p.at, reverse=True)
    return plays


@dataclass
class Suppression:
    """A candidate the DJ should probably not play, and WHY.

    The reason travels with it on purpose. A silently shortened candidate list
    teaches the DJ nothing and leaves Todd wondering why it never plays a song
    he likes; a stated reason it can say out loud is the whole point.
    """

    track_id: int
    reason: str  # recording_cooldown | work_cooldown | low_rating
    detail: str
    minutes_ago: float | None = None
    clears_in_minutes: float | None = None


@dataclass
class CandidateVerdict:
    allowed: list[int]
    suppressed: list[Suppression]
    recording_cooldown_minutes: float
    work_cooldown_minutes: float


def filter_candidates(
    db: Session,
    user_id: int,
    track_ids: list[int],
    recording_cooldown_minutes: float,
    work_cooldown_minutes: float,
    min_rating: int | None = None,
) -> CandidateVerdict:
    """Advisory filter over a candidate list. NOT a gate on explicit commands.

    Nothing here blocks playback. When Todd asks for a song by name he gets
    that song, cooldown or not — this exists so the DJ's own picks avoid
    repeating itself, and so it can explain the ones it passed over.
    """
    identities = build_identity_map(db)
    window = max(recording_cooldown_minutes, work_cooldown_minutes)
    recent = recent_plays_for(db, user_id, window, identities)

    newest_recording: dict[str, RecentPlay] = {}
    newest_work: dict[str, RecentPlay] = {}
    for play in recent:  # newest first, so first write wins
        newest_recording.setdefault(play.recording_id, play)
        newest_work.setdefault(play.work_id, play)

    ratings: dict[int, int] = {}
    if min_rating is not None:
        ratings = {
            track_id: rating
            for track_id, rating in db.query(TrackRating.track_id, TrackRating.rating)
            .filter(TrackRating.user_id == user_id)
            .all()
        }

    allowed: list[int] = []
    suppressed: list[Suppression] = []
    for track_id in track_ids:
        identity = identities.get(track_id)
        if identity is None:
            allowed.append(track_id)  # unknown to us; not ours to veto
            continue

        played = newest_recording.get(identity.recording_id)
        if played and played.minutes_ago < recording_cooldown_minutes:
            suppressed.append(
                Suppression(
                    track_id=track_id,
                    reason="recording_cooldown",
                    detail=(
                        f"this exact recording played {played.minutes_ago:.0f} min ago"
                    ),
                    minutes_ago=played.minutes_ago,
                    clears_in_minutes=round(
                        recording_cooldown_minutes - played.minutes_ago, 2
                    ),
                )
            )
            continue

        played = newest_work.get(identity.work_id)
        if played and played.minutes_ago < work_cooldown_minutes:
            suppressed.append(
                Suppression(
                    track_id=track_id,
                    reason="work_cooldown",
                    detail=(
                        f"another version of this song played "
                        f"{played.minutes_ago:.0f} min ago"
                    ),
                    minutes_ago=played.minutes_ago,
                    clears_in_minutes=round(
                        work_cooldown_minutes - played.minutes_ago, 2
                    ),
                )
            )
            continue

        if min_rating is not None:
            rating = ratings.get(track_id)
            if rating is not None and rating < min_rating:
                suppressed.append(
                    Suppression(
                        track_id=track_id,
                        reason="low_rating",
                        detail=f"rated {rating} star(s), below the requested {min_rating}",
                    )
                )
                continue

        allowed.append(track_id)

    return CandidateVerdict(
        allowed=allowed,
        suppressed=suppressed,
        recording_cooldown_minutes=recording_cooldown_minutes,
        work_cooldown_minutes=work_cooldown_minutes,
    )
