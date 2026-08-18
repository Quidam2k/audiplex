"""Import RFL identification verdicts into the repair overlay (#3037 Phase 4).

The deterministic ladder in tag_repair.py can only read what is already in the
file. For the residue — a bare title over a junk uploader account — the answer
has to come from outside, and that is what the RFL toolkit's identification
pass provides.

A MusicBrainz hit is not the same kind of evidence as a parsed title, though,
and the difference matters. MusicBrainz is matched largely on TITLE, so a
lookup for "stars" or "violet" will confidently return SOME recording with that
name — just not necessarily this one. Four of the medium-confidence verdicts in
the first batch were exactly that: an Arcane soundtrack cut identified as a Suzy
Bogguss country song, a Hank Green upload identified as a Linda Clifford disco
record. Both scored above 0.82.

So a verdict is applied when it is CORROBORATED by something already in the
file — the artist's name appearing in the title tag, or a channel that matches
it — and held for review when it is not. High confidence is trusted on its own;
below that, one external claim with nothing backing it is precisely the "wrong
artist silently poisons identity keys" case Todd warned about, and a blank is
the cheaper mistake.

Held verdicts are not discarded. Their proposals and evidence are written to the
overlay so the review queue carries what RFL learned; only `status` withholds
them from the catalog.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from audiplex import tag_repair
from audiplex.models import TrackTagRepair

IDENTIFIED = "identified"
UNIDENTIFIED = "unidentified"
NON_MUSIC = "non_music"

# Below this, a name is too short for "appears inside the title" to mean
# anything — two characters match by accident all the time.
_MIN_CORROBORATION_LENGTH = 3


@dataclass
class ImportCounts:
    applied: int = 0
    held_uncorroborated: int = 0
    held_low: int = 0
    held_unidentified: int = 0
    held_non_music: int = 0
    already_resolved: int = 0
    unknown_file: int = 0

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "held_uncorroborated": self.held_uncorroborated,
            "held_low": self.held_low,
            "held_unidentified": self.held_unidentified,
            "held_non_music": self.held_non_music,
            "already_resolved": self.already_resolved,
            "unknown_file": self.unknown_file,
        }


def corroborated(artist: str | None, title_tag: str | None, channel: str | None) -> bool:
    """Is anything already in the file saying the same name?

    Two ways to qualify: the performer is named in the video title (which is how
    "Cups ... - Anna Kendrick" and "Fun.: We Are Young" identify themselves), or
    the uploading channel matches (tag_repair's prefix rule, so "AshnikkoVEVO"
    counts and a channel buried mid-string does not).
    """
    if not artist:
        return False
    folded = tag_repair._comparable(artist)
    if len(folded) < _MIN_CORROBORATION_LENGTH:
        return False
    if title_tag and folded in tag_repair._comparable(title_tag):
        return True
    return tag_repair.channels_agree(tag_repair.read_channel(channel), artist)


def read_file_tags(file_path: str | None) -> tuple[str | None, str | None]:
    """(title tag, channel tag) straight off the file — the corroborating
    evidence has to come from the file itself, not from what the parser already
    concluded about it."""
    if not file_path or not Path(file_path).exists():
        return None, None
    try:
        import mutagen

        audio = mutagen.File(file_path, easy=True)
    except Exception:
        return None, None
    if audio is None:
        return None, None

    def first(key):
        try:
            value = audio.get(key)
        except Exception:
            return None
        return value[0] if value else None

    return first("title"), first("artist")


def _should_apply(row: dict, title_tag: str | None, channel: str | None) -> bool:
    if row.get("verdict") != IDENTIFIED:
        return False
    if not row.get("artist") or not row.get("title"):
        return False
    confidence = (row.get("confidence") or "").lower()
    if confidence == tag_repair.HIGH:
        return True
    if confidence != tag_repair.MEDIUM:
        return False
    return corroborated(row.get("artist"), title_tag, channel)


def load(path: str) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def import_verdicts(db: Session, rows: list[dict]) -> tuple[ImportCounts, list[str]]:
    """Fold RFL's verdicts into the overlay. Returns counts and a per-row log."""
    counts = ImportCounts()
    log: list[str] = []

    for row in rows:
        file_path = row.get("file_path")
        repair = (
            db.query(TrackTagRepair).filter(TrackTagRepair.file_path == file_path).first()
            if file_path
            else None
        )
        if repair is None:
            counts.unknown_file += 1
            log.append(f"  ?? no overlay row for {file_path!r} — skipped")
            continue

        if repair.status == tag_repair.APPLIED:
            # Folder consensus already settled these two Marillion tracks, and
            # RFL independently agrees. Nothing to do, and nothing to undo.
            counts.already_resolved += 1
            log.append(
                f"  == [{row.get('track_id')}] already resolved as "
                f"{repair.proposed_artist!r} via {repair.source}; RFL says "
                f"{row.get('artist')!r}"
            )
            continue

        verdict = row.get("verdict")
        confidence = (row.get("confidence") or "").lower()
        evidence = row.get("evidence") or ""

        title_tag, channel = read_file_tags(file_path)

        if _should_apply(row, title_tag, channel):
            repair.proposed_artist = row["artist"]
            repair.proposed_title = row["title"]
            repair.proposed_album = row.get("album")
            repair.confidence = tag_repair.HIGH
            repair.source = tag_repair.SOURCE_RFL
            repair.status = tag_repair.APPLIED
            repair.evidence = f"RFL {confidence}: {evidence}"
            counts.applied += 1
            log.append(f"  ++ [{row.get('track_id')}] {row['artist']} — {row['title']}")
            continue

        # Held. Keep what RFL found anyway so the queue carries its reasoning.
        if verdict == IDENTIFIED:
            repair.proposed_artist = row.get("artist") or repair.proposed_artist
            repair.proposed_title = row.get("title") or repair.proposed_title
            repair.proposed_album = row.get("album") or repair.proposed_album
            repair.source = tag_repair.SOURCE_RFL
            repair.confidence = confidence or repair.confidence
            if confidence == tag_repair.MEDIUM:
                counts.held_uncorroborated += 1
                reason = (
                    "nothing in the file corroborates this name — MusicBrainz "
                    "matches on title, so an uncorroborated hit may be a "
                    "different recording entirely"
                )
                log.append(
                    f"  -- [{row.get('track_id')}] HELD {row.get('artist')!r} "
                    "(uncorroborated)"
                )
            else:
                counts.held_low += 1
                reason = "RFL confidence below the bar to write without review"
                log.append(f"  -- [{row.get('track_id')}] HELD (low confidence)")
            repair.evidence = f"RFL {confidence}: {evidence} — HELD: {reason}"
        elif verdict == NON_MUSIC:
            counts.held_non_music += 1
            repair.evidence = (
                f"RFL: not music — {evidence}. Held per the #3040 ruling; "
                "classification belongs to #954."
            )
            log.append(f"  -- [{row.get('track_id')}] HELD (not music)")
        else:
            counts.held_unidentified += 1
            repair.evidence = f"RFL could not identify this: {evidence}"
            log.append(f"  -- [{row.get('track_id')}] HELD (unidentified)")

    return counts, log
