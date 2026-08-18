"""Weigh every file in the music catalog and report what repair would do.

READ-ONLY by construction. Nothing here opens a write transaction, so it can be
pointed at the live database while the server is running. Applying the verdicts
is a separate, explicit step — Todd's rule is that a wrong artist is worse than
a blank one, so the proposals get looked at before they get written.

`unresolved_export` is the other half: the residual that no deterministic parse
can settle goes out as JSON for the RFL toolkit's identification pass
(rfl_identify_track / rfl_enrich_scan), whose verdicts come back through the
same overlay table with source='rfl'.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import mutagen
from sqlalchemy.orm import Session

from audiplex import tag_repair
from audiplex.models import Album, Artist, Track


@dataclass
class Row:
    track_id: int
    file_path: str
    current_artist: str
    current_title: str
    duration: float
    channel: str | None
    source_url: str | None
    proposal: tag_repair.Proposal

    @property
    def changes_artist(self) -> bool:
        return bool(self.proposal.artist) and self.proposal.artist != self.current_artist

    @property
    def changes_title(self) -> bool:
        return bool(self.proposal.title) and self.proposal.title != self.current_title


def _tag(audio, key: str) -> str | None:
    try:
        value = audio.get(key)
    except Exception:
        return None
    if not value:
        return None
    first = value[0]
    return first.strip() if isinstance(first, str) else first


def collect(db: Session) -> list[Row]:
    """One proposal per track in the catalog, read straight off the files."""
    query = (
        db.query(Track.id, Track.title, Track.file_path, Track.duration_seconds, Artist.name)
        .join(Artist, Artist.id == Track.artist_id)
        .order_by(Track.id)
    )

    rows: list[Row] = []
    for track_id, title, file_path, duration, artist_name in query.all():
        title_tag = artist_tag = comment = None
        if Path(file_path).exists():
            try:
                audio = mutagen.File(file_path, easy=True)
            except Exception:
                audio = None
            if audio is not None:
                title_tag = _tag(audio, "title")
                artist_tag = _tag(audio, "artist")
                comment = _tag(audio, "comment")

        proposal = tag_repair.propose(
            title_tag=title_tag,
            artist_tag=artist_tag,
            filename_stem=Path(file_path).stem,
            comment=comment,
        )
        rows.append(
            Row(
                track_id=track_id,
                file_path=file_path,
                current_artist=artist_name or "",
                current_title=title or "",
                duration=float(duration or 0.0),
                channel=artist_tag,
                source_url=comment if tag_repair.is_youtube_rip(comment) else None,
                proposal=proposal,
            )
        )
    return rows


def summarize(rows: list[Row]) -> dict:
    by_confidence = Counter(r.proposal.confidence for r in rows)
    auto = [r for r in rows if r.proposal.auto_applicable]
    return {
        "tracks": len(rows),
        "by_confidence": dict(by_confidence),
        "auto_applicable": len(auto),
        "held_for_review": len(rows) - len(auto),
        "artist_changes": sum(1 for r in auto if r.changes_artist),
        "title_changes": sum(1 for r in auto if r.changes_title),
        "distinct_artists_after": len({r.proposal.artist for r in auto if r.proposal.artist}),
    }


def render(rows: list[Row]) -> str:
    """The full proposed-change report, one line per track."""
    out: list[str] = []
    summary = summarize(rows)
    out.append("=== TAG REPAIR DRY RUN (#3037) — no rows written ===")
    out.append("")
    out.append(f"tracks weighed        : {summary['tracks']}")
    for level in (tag_repair.HIGH, tag_repair.MEDIUM, tag_repair.LOW, tag_repair.UNRESOLVED):
        out.append(f"  {level:<20}: {summary['by_confidence'].get(level, 0)}")
    out.append(f"auto-applicable (high): {summary['auto_applicable']}")
    out.append(f"held for review       : {summary['held_for_review']}")
    out.append(f"artist would change   : {summary['artist_changes']}")
    out.append(f"title would change    : {summary['title_changes']}")
    out.append(f"distinct artists after: {summary['distinct_artists_after']}")
    out.append("")

    for level in (tag_repair.HIGH, tag_repair.MEDIUM, tag_repair.LOW, tag_repair.UNRESOLVED):
        group = [r for r in rows if r.proposal.confidence == level]
        if not group:
            continue
        verb = "WOULD APPLY" if level == tag_repair.HIGH else "HELD FOR REVIEW"
        out.append(f"--- {level.upper()} ({len(group)}) — {verb} ---")
        for row in group:
            out.append(
                f"[{row.track_id:>4}] {row.current_artist or '(blank)'} / {row.current_title}"
            )
            out.append(
                f"        -> {row.proposal.artist or '(no artist)'} / "
                f"{row.proposal.title or '(no title)'}"
            )
            out.append(f"        channel={row.channel!r}  why={row.proposal.evidence}")
        out.append("")

    return "\n".join(out)


def unresolved_export(rows: list[Row]) -> list[dict]:
    """The residual, as JSON for the RFL identification pass."""
    return [
        {
            "track_id": row.track_id,
            "file_path": row.file_path,
            "current_title": row.current_title,
            "title_tag": row.proposal.title,
            "channel": row.channel,
            "youtube_url": row.source_url,
            "duration_seconds": round(row.duration, 1),
            "confidence": row.proposal.confidence,
            "why_unresolved": row.proposal.evidence,
        }
        for row in rows
        if not row.proposal.auto_applicable
    ]


def write_export(rows: list[Row], path: str) -> int:
    payload = unresolved_export(rows)
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(payload)
