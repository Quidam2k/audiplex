"""#5908: rating floor + playing-at (pure logic over a Session)."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from audiplex import dj_learn
from audiplex.models import Artist, PlayStat, RatingFloor, Track, TrackRating, _utcnow

log = logging.getLogger(__name__)

NOTE_MAX = 500  # TrackRating.note is String(500)
REASON_MAX = 200  # RatingFloor.reason is String(200)


def _add_note(old_note: str | None, tag: str) -> str:
    """Append tag to the note; truncate the old note (never the tag) past NOTE_MAX."""
    if not old_note:
        return tag[:NOTE_MAX]
    new_note = old_note + " " + tag
    if len(new_note) <= NOTE_MAX:
        return new_note
    keep = NOTE_MAX - len(tag) - 1
    if keep <= 0:
        return tag[:NOTE_MAX]
    return old_note[:keep] + " " + tag


def apply_floor(
    db: Session,
    owner_id: int,
    track_ids: list[int],
    min_stars: float,
    source_id: str,
    reason: str,
    persona: str,
) -> dict:
    """Raise the owner's rating to min_stars where it is lower; never lower one. #5908"""
    wanted = set(track_ids)
    known = set(db.scalars(select(Track.id).where(Track.id.in_(wanted))).all()) if wanted else set()
    unknown = sorted(wanted - known)

    tag = f"[{persona.strip() or 'DJ'}: {reason.strip()}]"
    results: list[dict] = []
    raised: list[int] = []

    for tid in sorted(known):
        row = db.scalars(
            select(TrackRating).where(
                TrackRating.user_id == owner_id, TrackRating.track_id == tid
            )
        ).first()
        was = row.rating if row is not None else None

        # One decision per (track, source): a repeat drop changes nothing.
        seen = db.scalars(
            select(RatingFloor.id).where(
                RatingFloor.track_id == tid, RatingFloor.source_id == source_id
            )
        ).first()
        if seen is not None:
            results.append({"track_id": tid, "outcome": "already", "was": was, "now": was})
            continue

        if row is not None and row.rating >= min_stars:
            outcome = "kept"
        else:
            earlier_raise = db.scalars(
                select(RatingFloor)
                .where(RatingFloor.track_id == tid, RatingFloor.outcome == "raised")
                .order_by(RatingFloor.created_at.desc(), RatingFloor.id.desc())
            ).first()
            if (earlier_raise is not None and row is not None and earlier_raise.new_rating is not None
                    and row.rating < earlier_raise.new_rating):
                # Lowered by hand below what a floor set: his rating wins.
                outcome = "vetoed"
            else:
                outcome = "raised"

        try:
            with db.begin_nested():  # a concurrent retry of the same source loses cleanly
                now_rating = _decide(db, row, owner_id, tid, outcome, min_stars, tag, was, source_id, reason)
        except IntegrityError:
            results.append({"track_id": tid, "outcome": "already", "was": was, "now": was})
            continue
        if outcome == "raised":
            raised.append(tid)
        log.info(
            "rating floor: track=%s source=%s %s->%s %s",
            tid, source_id, was, now_rating, outcome,
        )
        results.append({"track_id": tid, "outcome": outcome, "was": was, "now": now_rating})

    db.commit()

    restored: list[int] = []
    if min_stars >= dj_learn.LOVE_MIN_STARS:
        for tid in raised:
            try:
                if dj_learn.restore(db, tid):
                    restored.append(tid)
            except Exception:  # the ratings are committed; a restore failure must not 500 them
                log.exception("rating floor: dj_learn.restore failed for track %s", tid)

    return {"results": results, "unknown": unknown, "restored": restored}


def _decide(db, row, owner_id, tid, outcome, min_stars, tag, was, source_id, reason):
    """Write one decision (rating change if raised, plus its log row) and flush. #5908"""
    if outcome == "raised":
        if row is None:
            row = TrackRating(
                user_id=owner_id,
                track_id=tid,
                rating=min_stars,
                note=_add_note(None, tag),
                updated_at=_utcnow(),
            )
            db.add(row)
        else:
            row.rating = min_stars
            row.note = _add_note(row.note, tag)
            row.updated_at = _utcnow()
    now_rating = row.rating if row is not None else None
    db.add(
        RatingFloor(
            track_id=tid,
            source_id=source_id,
            outcome=outcome,
            old_rating=was,
            new_rating=now_rating,
            reason=reason[:REASON_MAX],
        )
    )
    db.flush()
    return now_rating


def playing_at(
    db: Session,
    owner_id: int,
    at: datetime,
    max_lookback_s: float = 4 * 3600,
) -> dict | None:
    """What the owner was playing at `at`, or None. #5908"""
    if at.tzinfo is not None:
        # Stored timestamps are naive UTC.
        at = at.astimezone(timezone.utc).replace(tzinfo=None)

    owner_match = or_(PlayStat.user_id == owner_id, PlayStat.user_id.is_(None))

    start = db.scalars(
        select(PlayStat)
        .where(
            owner_match,
            PlayStat.event == "start",
            PlayStat.timestamp <= at,
            PlayStat.timestamp >= at - timedelta(seconds=max_lookback_s),
        )
        .order_by(PlayStat.timestamp.desc(), PlayStat.id.desc())
    ).first()
    if start is None:
        return None

    track = db.get(Track, start.track_id)
    if track is None:
        return None

    # Window ends at the next owner event of any kind/track, else at the track's length.
    nxt = db.scalars(
        select(PlayStat)
        .where(owner_match, or_(  # same-second events count, ordered by id
            PlayStat.timestamp > start.timestamp,
            and_(PlayStat.timestamp == start.timestamp, PlayStat.id > start.id)))
        .order_by(PlayStat.timestamp.asc(), PlayStat.id.asc())
    ).first()
    if nxt is not None:
        ends_at = nxt.timestamp
    else:
        if track.end_trim_seconds is not None:
            length = track.end_trim_seconds
        else:
            length = track.duration_seconds or 0
        ends_at = start.timestamp + timedelta(seconds=length)

    if not (start.timestamp <= at <= ends_at):
        return None

    artist = db.get(Artist, track.artist_id) if track.artist_id else None
    return {
        "track_id": track.id,
        "title": track.title,
        "artist": (artist.name if artist is not None else "") or "",
        "started_at": start.timestamp.isoformat(),
        "ends_at": ends_at.isoformat(),
    }
