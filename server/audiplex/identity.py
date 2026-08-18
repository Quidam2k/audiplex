"""Canonical song identity — deliberately TWO keys (#943 / #947 / #948).

    recording_id — the same AUDIO. A local copy and a streamed copy of one
                   file are one thing. RATINGS and PLAY STATISTICS bind here.
    work_id      — the same SONG in any version: live, remaster, single edit,
                   a different album. COOLDOWN binds here.

They are not one key, and the split is the whole point. Loving the studio cut
while finding the live version thin is an ordinary opinion; a single key would
silently average it away, which is exactly the loss #3028 avoided by refusing
to convert dj_rate's vocabulary. Cooldown wants the loose key — Todd doesn't
want to hear a song twice in twenty minutes in ANY form. Ratings want the
strict one. So: built once, keyed twice.

Covers by other artists stay distinct under both keys, because the artist is
part of both. That is intentional — someone else's version of a song is a
different thing to like and a different thing to hear.

No MusicBrainz IDs exist in this library, so identity is DERIVED from
(title, artist, duration, file_hash). That makes it a heuristic, not a fact;
see RECORDING_TOLERANCE_SECONDS and strip_qualifiers for where it can be wrong.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy.orm import Session

from audiplex.models import Artist, Track

# Two rips of one recording rarely agree to the millisecond — encoder padding,
# gapless trimming, and tag-reported vs measured duration all disagree slightly.
# Three seconds absorbs that without merging a radio edit into an album cut.
RECORDING_TOLERANCE_SECONDS = 3.0

# Words that mark a suffix as a VERSION of a work rather than part of its name.
# Used only for the dash form ("Barracuda - 2004 Remaster"), where stripping
# unconditionally would eat real titles like "Life - A Portrait".
_QUALIFIER_WORDS = {
    "remaster",
    "remastered",
    "remix",
    "mix",
    "version",
    "edit",
    "live",
    "acoustic",
    "demo",
    "mono",
    "stereo",
    "instrumental",
    "reprise",
    "radio",
    "single",
    "album",
    "extended",
    "deluxe",
    "bonus",
    "take",
    "session",
    "sessions",
    "unplugged",
    "cut",
}

_BRACKETED = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_DASH_SUFFIX = re.compile(r"\s+[-–—]\s+(?P<tail>[^-–—]+)$")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")
_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+")


def normalize(text: str | None) -> str:
    """Fold a title or artist name to a comparable form.

    Accents dropped, '&' spelled out, punctuation removed, whitespace
    collapsed, leading article dropped so "The Beatles" and "Beatles" are one
    artist.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = folded.lower().replace("&", " and ")
    folded = _PUNCT.sub(" ", folded)
    folded = _SPACES.sub(" ", folded).strip()
    return _LEADING_ARTICLE.sub("", folded)


def strip_qualifiers(title: str | None) -> str:
    """"Barracuda (2004 Remaster)" -> "barracuda". The work-level title.

    Bracketed groups go unconditionally: at work level a "(feat. X)" or a
    "(Live at Budokan)" is the same song either way, which is what cooldown
    cares about. A trailing " - <something>" goes only when <something> reads
    like a version marker, because the dash form is also how ordinary titles
    are punctuated.

    If stripping would leave nothing — a track genuinely called "(Untitled)" —
    the original is kept. An empty work key would collapse every such track
    into one song.
    """
    if not title:
        return ""
    stripped = _BRACKETED.sub("", title).strip()
    match = _DASH_SUFFIX.search(stripped)
    if match:
        tail_words = set(normalize(match.group("tail")).split())
        if tail_words & _QUALIFIER_WORDS:
            stripped = stripped[: match.start()].strip()
    normalized = normalize(stripped)
    return normalized or normalize(title)


def work_key(title: str | None, artist: str | None) -> str:
    """The song, whatever the version."""
    return f"work:{normalize(artist)}|{strip_qualifiers(title)}"


@dataclass(frozen=True)
class TrackIdentity:
    track_id: int
    recording_id: str
    work_id: str


class _DisjointSet:
    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_b] = root_a


@dataclass(frozen=True)
class _TrackRow:
    id: int
    title: str
    artist: str
    duration: float
    file_hash: str | None


def _load_rows(db: Session) -> list[_TrackRow]:
    rows = (
        db.query(
            Track.id, Track.title, Track.duration_seconds, Track.file_hash, Artist.name
        )
        .join(Artist, Artist.id == Track.artist_id)
        .all()
    )
    return [
        _TrackRow(
            id=track_id,
            title=title or "",
            artist=artist or "",
            duration=float(duration or 0.0),
            file_hash=(file_hash or None),
        )
        for track_id, title, duration, file_hash, artist in rows
    ]


def build_identity_map(db: Session) -> dict[int, TrackIdentity]:
    """Identity for every track in the library, computed fresh.

    Deliberately not cached and not stored on the row. The library is a few
    hundred tracks, so this is one query and a linear pass; a cache would buy
    microseconds and cost a staleness bug the first time #943 syncs a phone's
    catalog mid-session.
    """
    return _identities_for(_load_rows(db))


def _identities_for(rows: list[_TrackRow]) -> dict[int, TrackIdentity]:
    sets = _DisjointSet()
    for row in rows:
        sets.find(row.id)

    # Same bytes is the strongest evidence there is: one recording, two files.
    by_hash: dict[str, list[int]] = {}
    for row in rows:
        if row.file_hash:
            by_hash.setdefault(row.file_hash, []).append(row.id)
    for ids in by_hash.values():
        for other in ids[1:]:
            sets.union(ids[0], other)

    # Otherwise: same title, same artist, near-identical length. Sorting and
    # unioning neighbours means 60s/62s/64s all chain into one recording even
    # though the ends are 4s apart. That is single-link clustering, and it is
    # accepted here — a run of near-identical durations under one title really
    # is one recording far more often than it is three.
    by_title: dict[tuple[str, str], list[_TrackRow]] = {}
    for row in rows:
        key = (normalize(row.artist), normalize(row.title))
        by_title.setdefault(key, []).append(row)
    for bucket in by_title.values():
        ordered = sorted(bucket, key=lambda r: r.duration)
        for previous, current in zip(ordered, ordered[1:]):
            if current.duration - previous.duration <= RECORDING_TOLERANCE_SECONDS:
                sets.union(previous.id, current.id)

    clusters: dict[int, list[_TrackRow]] = {}
    for row in rows:
        clusters.setdefault(sets.find(row.id), []).append(row)

    identities: dict[int, TrackIdentity] = {}
    for members in clusters.values():
        recording_id = _label(members)
        for row in members:
            identities[row.id] = TrackIdentity(
                track_id=row.id,
                recording_id=recording_id,
                work_id=work_key(row.title, row.artist),
            )
    return identities


def _label(members: list[_TrackRow]) -> str:
    """A stable name for one recording cluster.

    Derived from the cluster's own content, never from a row id, so adding a
    second copy of a track later does not rename the recording out from under
    the ratings already bound to it.
    """
    hashes = sorted(m.file_hash for m in members if m.file_hash)
    if hashes:
        return f"rec:hash:{hashes[0]}"
    anchor = min(members, key=lambda r: (r.duration, r.id))
    return (
        f"rec:{normalize(anchor.artist)}|{normalize(anchor.title)}"
        f"|{int(round(anchor.duration))}"
    )


def identity_for(db: Session, track_id: int) -> TrackIdentity | None:
    """One track's identity. Still builds the whole map — see
    build_identity_map on why that is the right trade at this size."""
    return build_identity_map(db).get(track_id)


def group_ids_by_recording(identities: dict[int, TrackIdentity]) -> dict[str, list[int]]:
    return _group(identities, lambda i: i.recording_id)


def group_ids_by_work(identities: dict[int, TrackIdentity]) -> dict[str, list[int]]:
    return _group(identities, lambda i: i.work_id)


def _group(identities: dict[int, TrackIdentity], key) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for identity in identities.values():
        grouped.setdefault(key(identity), []).append(identity.track_id)
    return {name: sorted(ids) for name, ids in grouped.items()}
