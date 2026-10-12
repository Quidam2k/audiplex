"""Read-only, sourced song memory for the DJ (Pantheon #3984).

Stored prose is returned verbatim. Love notes, private pair notes, and rating
notes never enter the public results.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Iterator

try:
    from audiplex_mcp.dj_toolkit import LOVE_PREFIX
except Exception:
    LOVE_PREFIX = "Todd loves this"

WEIGHTS = {  # Tunable.
    "pair_note": 10,
    "sample_link": 6,
    "cover_link": 6,
    "same_writer": 4,
    "shared_theme": 3,
    "shared_tag": 3,
    "shared_bucket": 3,
    "harmonic": 2,
    "energy": 1,
    "same_year": 1,
}
COOLDOWN_HOURS = 12
SKIP_TAGS = frozenset({"loved", "favorite"})
NEGATIVE = re.compile(
    r"\b(never|don'?t|do not|avoid|not after|not before|no good|"
    r"bad (?:fit|segue|transition)|clash(?:es)?)\b",
    re.I,
)

_STOPWORDS = frozenset(
    """
    a an the and or but if as at by for from in into of on onto to with
    without about above after before below between through under over
    during up down out off again further then once here there when where
    why how all any both each few more most other some such no nor not
    only own same so than too very can will just should now is are was
    were be been being have has had do does did it its this that these
    those i me my we us our you your he him his she her they them their
    who whom which what
    """.split()
) | frozenset(  # music-press filler: shared by unrelated songs, never a theme
    """
    really also song songs single singles album albums track tracks lead
    singer singers vocal vocals band group rolling stone stones billboard
    released release written writer wrote described called said says like
    one two three first second new hit hits chart charts version versions
    record records recorded recording video number uk us top music musical
    later year years time times made make well much many way
    """.split()
)
WEAK_KINDS = frozenset({"same_year", "energy"})  # never enough on their own
_WORDS = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_CAMELOT = re.compile(r"^(1[0-2]|[1-9])([AB])$", re.I)
_QUOTES = re.compile(r'"([^"]+)"|“([^”]+)”')

Fact = dict[str, str]
Track = dict[str, Any]

_store_mod: ModuleType | None = None
_store_path: Path | None = None
_library_stamp: tuple[Any, ...] | None = None
_library_tracks: dict[int, Track] = {}
_library_keys: dict[str, list[int]] = {}


def _audiplex_db_path() -> Path:
    return Path(
        os.environ.get("AUDIPLEX_DB_PATH")
        or Path(__file__).resolve().parent.parent / "server" / "audiplex.db"
    )


def _rfl_notes_db_path() -> Path:
    return Path(
        os.environ.get("RFL_DJ_NOTES_DB")
        or "Q:/Development/radio_free_luna/data/dj_notes.db"
    )


def _rfl_store_path() -> Path:
    return Path(
        os.environ.get("RFL_STORE_PY")
        or "Q:/Development/radio_free_luna/src/notes/store.py"
    )


def _reset_cache() -> None:
    """Discard the imported key helper and library index."""
    global _store_mod, _store_path, _library_stamp
    global _library_tracks, _library_keys
    _store_mod = None
    _store_path = None
    _library_stamp = None
    _library_tracks = {}
    _library_keys = {}


def _rfl_store() -> ModuleType:
    global _store_mod, _store_path
    path = _rfl_store_path().resolve()
    if _store_mod is not None and _store_path == path:
        return _store_mod
    if not path.is_file():
        raise FileNotFoundError(path)
    name = "_audiplex_song_context_rfl_store"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load RFL store: {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    old_bytecode = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
        if not callable(getattr(module, "song_key", None)):
            raise ImportError(f"RFL store has no song_key: {path}")
    except Exception:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    finally:
        sys.dont_write_bytecode = old_bytecode
    _store_mod, _store_path = module, path
    return module


def _open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=0.5
    )
    conn.row_factory = sqlite3.Row
    return conn


def _library(
    conn: sqlite3.Connection, path: Path, store: ModuleType
) -> tuple[dict[int, Track], dict[str, list[int]]]:
    global _library_stamp, _library_tracks, _library_keys
    stamp = (path.resolve(), path.stat().st_mtime_ns, _store_path)
    if stamp != _library_stamp:
        tracks: dict[int, Track] = {}
        keys: dict[str, list[int]] = {}
        rows = conn.execute(
            """
            SELECT t.id, t.title, COALESCE(a.name, '') AS artist,
                   t.bpm, t.musical_key, t.energy
            FROM tracks t
            LEFT JOIN artists a ON a.id = t.artist_id
            WHERE t.content_kind = 'music' OR t.content_kind IS NULL
            ORDER BY t.id
            """
        )
        for row in rows:
            track = dict(row)
            track["key"] = store.song_key(track["artist"], track["title"])
            tracks[track["id"]] = track
            keys.setdefault(track["key"], []).append(track["id"])
        _library_tracks, _library_keys = tracks, keys
        _library_stamp = stamp
    return _library_tracks, _library_keys


@dataclass
class _Memory:
    aud: sqlite3.Connection
    rfl: sqlite3.Connection | None
    buckets: sqlite3.Connection | None
    store: ModuleType
    tracks: dict[int, Track]
    keys: dict[str, list[int]]
    fact_cache: dict[str, list[Fact]] = field(default_factory=dict)
    tag_cache: dict[int, list[str]] = field(default_factory=dict)
    bucket_cache: dict[int, list[tuple[int, str]]] = field(default_factory=dict)


@contextmanager
def _memory() -> Iterator[_Memory]:
    connections: list[sqlite3.Connection] = []
    try:
        store = _rfl_store()
        path = _audiplex_db_path()
        aud = _open_db(path)
        connections.append(aud)
        tracks, keys = _library(aud, path, store)
        rfl = None
        rfl_path = _rfl_notes_db_path()
        if rfl_path.is_file():
            rfl = _open_db(rfl_path)
            connections.append(rfl)
        bucket_conn = None
        try:
            from audiplex_mcp.buckets import _db_path
        except ImportError:
            _db_path = None
        if _db_path is not None:
            bucket_path = Path(_db_path())
            if bucket_path.is_file():
                bucket_conn = _open_db(bucket_path)
                connections.append(bucket_conn)
        yield _Memory(aud, rfl, bucket_conn, store, tracks, keys)
    finally:
        for conn in reversed(connections):
            conn.close()


def _error(function: str, exc: Exception) -> None:
    print(f"song_context.{function}: {exc!r}", file=sys.stderr)


def _reason(kind: str, text: str, source: str, url: str = "") -> Fact:
    return {"kind": kind, "text": text, "source": source, "url": url}


def _row_fact(row: sqlite3.Row) -> Fact:
    return _reason(row["kind"], row["text"], row["source_name"], row["source_url"])


def _two_sided(kind: str, a: Fact, b: Fact) -> Fact:
    return _reason(
        kind,
        a["text"] + " / " + b["text"],
        a["source"] + " / " + b["source"],
        a["url"] + " / " + b["url"],
    )


def _safe_note(note: str) -> bool:
    folded = note.casefold()
    return (
        not note.lstrip().casefold().startswith(LOVE_PREFIX.casefold())
        and "[private]" not in folded
    )


def _skip_tag(tag: str) -> bool:
    folded = tag.strip().casefold()
    return folded in SKIP_TAGS or folded.endswith("-star-verbal")


def _facts(memory: _Memory, key: str) -> list[Fact]:
    if key not in memory.fact_cache:
        result: list[Fact] = []
        seen: set[str] = set()
        if memory.rfl is not None:
            rows = memory.rfl.execute(
                """
                SELECT f.kind, f.text, f.source_name, f.source_url
                FROM facts f JOIN songs s ON s.key = f.song_key
                WHERE s.key = ? AND s.status IN ('ok', 'research')
                ORDER BY f.rank, f.id
                """,
                (key,),
            )
            for row in rows:
                text = row["text"]
                if text and text not in seen:
                    seen.add(text)
                    result.append(_row_fact(row))
        memory.fact_cache[key] = result
    return memory.fact_cache[key]


def _tags(memory: _Memory, track_id: int) -> list[str]:
    if track_id not in memory.tag_cache:
        memory.tag_cache[track_id] = [
            row["tag"]
            for row in memory.aud.execute(
                "SELECT tag FROM dj_track_tags WHERE track_id = ? ORDER BY tag",
                (track_id,),
            )
        ]
    return memory.tag_cache[track_id]


def _buckets(memory: _Memory, track_id: int) -> list[tuple[int, str]]:
    if track_id not in memory.bucket_cache:
        result = []
        if memory.buckets is not None:
            result = [
                (row["id"], row["name"])
                for row in memory.buckets.execute(
                    """
                    SELECT b.id, b.name
                    FROM buckets b JOIN bucket_tracks t ON t.bucket_id = b.id
                    WHERE t.track_id = ?
                    ORDER BY b.name, b.id
                    """,
                    (track_id,),
                )
            ]
        memory.bucket_cache[track_id] = result
    return memory.bucket_cache[track_id]


def _pair_notes(
    memory: _Memory, track_a: int, track_b: int | None = None
) -> list[sqlite3.Row]:
    sql = (
        "SELECT track_b, note FROM dj_pair_notes WHERE track_a = ?"
        + (" AND track_b = ?" if track_b is not None else "")
        + " ORDER BY created_at DESC, id DESC"
    )
    params = (track_a, track_b) if track_b is not None else (track_a,)
    return [
        row
        for row in memory.aud.execute(sql, params)
        if _safe_note(row["note"])
    ]


def _context(memory: _Memory, track_id: int) -> dict[str, Any] | None:
    track = memory.tracks.get(track_id)
    if track is None:
        return None
    facts = [
        fact
        for fact in _facts(memory, track["key"])
        if fact["kind"] != "artist" and len(fact["text"]) <= 280
    ][:4]
    rating = memory.aud.execute(
        """
        SELECT rating FROM track_ratings WHERE track_id = ?
        ORDER BY updated_at DESC, id DESC LIMIT 1
        """,
        (track_id,),
    ).fetchone()
    tags = _tags(memory, track_id)
    measured = {
        name: track[column]
        for name, column in (
            ("bpm", "bpm"),
            ("key", "musical_key"),
            ("energy", "energy"),
        )
        if track[column] is not None
    }
    return {
        "track_id": track_id,
        "title": track["title"],
        "artist": track["artist"],
        "facts": facts,
        "measured": measured,
        "todd": {
            "stars": float(rating["rating"]) if rating is not None else None,
            "loved": any(tag.casefold() == "loved" for tag in tags),
            "tags": [tag for tag in tags if not _skip_tag(tag)],
        },
        "pair_notes": [
            {"to": row["track_b"], "note": row["note"]}
            for row in _pair_notes(memory, track_id)[:5]
        ],
    }


def _writers(text: str) -> set[str]:
    text = re.sub(r"^\s*Written\s+by\s+", "", text, flags=re.I)
    result = set()
    for part in re.split(r",|\s+and\s+|&|;", text, flags=re.I):
        name = " ".join(part.strip().rstrip(".").split()).casefold()
        if len(_WORDS.findall(name)) >= 2:
            result.add(name)
    return result


def _words(text: str) -> list[str]:
    return _WORDS.findall(text.casefold())


def _bigrams(text: str, excluded: set[str]) -> set[tuple[str, str]]:
    words = [
        word for word in _words(text)
        if word not in _STOPWORDS and word not in excluded
        and len(word) >= 3 and not word.isdigit()
    ]
    return set(zip(words, words[1:]))


def _identity_words(*tracks: Track) -> set[str]:
    return {
        word
        for track in tracks
        for field_name in ("title", "artist")
        for word in _words(track[field_name])
    }


def _mentions_song(text: str, track: Track) -> bool:
    artist = track["artist"].strip()
    title = track["title"].casefold()
    quoted = [a or b for a, b in _QUOTES.findall(text)]
    return (
        len(artist) >= 3
        and artist.casefold() in text.casefold()
        and any(value.casefold() == title for value in quoted)
    )


def _year(text: str) -> str | None:
    match = _YEAR.search(text)
    return match.group() if match else None


def _compatible_codes(code: str | None) -> tuple[str, ...]:
    match = _CAMELOT.fullmatch((code or "").strip())
    if match is None:
        return ()
    number = int(match[1])
    letter = match[2].upper()
    return (
        f"{number}{letter}",
        f"{(number - 2) % 12 + 1}{letter}",
        f"{number % 12 + 1}{letter}",
        f"{number}{'B' if letter == 'A' else 'A'}",
    )


def _harmonic(a: Track, b: Track) -> bool:
    if a["bpm"] is None or b["bpm"] is None:
        return False
    code = (b["musical_key"] or "").strip().upper()
    if code not in _compatible_codes(a["musical_key"]):
        return False
    bpm_a, bpm_b = float(a["bpm"]), float(b["bpm"])
    return (
        bpm_a > 0
        and bpm_b > 0
        and any(
            abs(bpm_a - bpm_b * factor) <= bpm_a * 0.06
            for factor in (0.5, 1.0, 2.0)
        )
    )


def _link(memory: _Memory, track_a: int, track_b: int) -> list[Fact]:
    a, b = memory.tracks.get(track_a), memory.tracks.get(track_b)
    if a is None or b is None:
        return []
    notes = _pair_notes(memory, track_a, track_b)
    for row in notes:
        if NEGATIVE.search(row["note"]):
            return [_reason("veto", row["note"], "DJ pair note")]

    reasons = [_reason("pair_note", row["note"], "DJ pair note") for row in notes]
    facts_a, facts_b = _facts(memory, a["key"]), _facts(memory, b["key"])
    artists = {
        " ".join(track["artist"].split()).casefold() for track in (a, b)
    }
    same_artist = len(artists) == 1  # one act sharing its own writers is not news
    writer_reason = None if same_artist else next(
        (
            _two_sided("same_writer", fa, fb)
            for fa in facts_a if fa["kind"] == "writers"
            for fb in facts_b if fb["kind"] == "writers"
            if (_writers(fa["text"]) & _writers(fb["text"])) - artists
        ),
        None,
    )
    if writer_reason is not None:
        reasons.append(writer_reason)

    for kind, fact_kinds in (
        ("sample_link", {"samples"}),
        ("cover_link", {"cover_of", "covers"}),
    ):
        evidence = next(
            (
                fact
                for facts, target in ((facts_a, b), (facts_b, a))
                for fact in facts
                if fact["kind"] in fact_kinds
                and _mentions_song(fact["text"], target)
            ),
            None,
        )
        if evidence is not None:
            reasons.append(_reason(
                kind, evidence["text"], evidence["source"], evidence["url"]
            ))

    excluded = _identity_words(a, b)
    theme_reason = next(
        (
            {**_two_sided("shared_theme", fa, fb), "match": " ".join(min(shared))}
            for fa in facts_a if fa["kind"] == "theme"
            for fb in facts_b if fb["kind"] == "theme"
            for shared in [_bigrams(fa["text"], excluded) & _bigrams(fb["text"], excluded)]
            if shared
        ),
        None,
    )
    if theme_reason is not None:
        reasons.append(theme_reason)

    tags_b = {
        tag.casefold() for tag in _tags(memory, track_b) if not _skip_tag(tag)
    }
    for tag in _tags(memory, track_a):
        if not _skip_tag(tag) and tag.casefold() in tags_b:
            reasons.append(_reason(
                "shared_tag", f"both tagged '{tag}'", "Todd's tags"
            ))

    buckets_b = {bucket_id for bucket_id, _ in _buckets(memory, track_b)}
    for bucket_id, name in _buckets(memory, track_a):
        if bucket_id in buckets_b:
            reasons.append(_reason(
                "shared_bucket", f"both in the '{name}' bucket", "DJ buckets"
            ))

    if _harmonic(a, b):
        reasons.append(_reason(
            "harmonic",
            f"keys {a['musical_key']} -> {b['musical_key']}, "
            f"{a['bpm']} -> {b['bpm']} BPM (measured)",
            "Audiplex analysis",
        ))
    if (
        a["energy"] is not None
        and b["energy"] is not None
        and abs(a["energy"] - b["energy"]) <= 10
    ):
        reasons.append(_reason(
            "energy",
            f"energy {a['energy']} -> {b['energy']} (measured)",
            "Audiplex analysis",
        ))

    release_a = next(
        (f for f in facts_a if f["kind"] == "release" and _year(f["text"])),
        None,
    )
    release_b = next(
        (f for f in facts_b if f["kind"] == "release" and _year(f["text"])),
        None,
    )
    if (
        release_a is not None and release_b is not None
        and _year(release_a["text"]) == _year(release_b["text"])
    ):
        reasons.append(_two_sided("same_year", release_a, release_b))
    return reasons


def _banned(memory: _Memory) -> set[int]:
    banned = set()
    keys = set()
    for row in memory.aud.execute(
        """
        SELECT b.track_id, t.title, a.name AS artist
        FROM dj_bans b
        LEFT JOIN tracks t ON t.id = b.track_id
        LEFT JOIN artists a ON a.id = t.artist_id
        """
    ):
        banned.add(row["track_id"])
        if row["title"] is not None:
            keys.add(memory.store.song_key(row["artist"], row["title"]))
    for key in keys:
        banned.update(memory.keys.get(key, ()))
    return banned


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fact_candidates(
    memory: _Memory,
    kinds: tuple[str, ...],
    patterns: tuple[str, ...],
    limit: int | None = None,
) -> list[sqlite3.Row]:
    if memory.rfl is None:
        return []
    kind_marks = ",".join("?" for _ in kinds)
    filters = " AND ".join("f.text LIKE ? ESCAPE '\\'" for _ in patterns)
    sql = f"""
        SELECT f.song_key, f.kind, f.text, f.source_name, f.source_url,
               s.artist, s.title
        FROM facts f JOIN songs s ON s.key = f.song_key
        WHERE s.status IN ('ok', 'research')
          AND f.kind IN ({kind_marks}) AND {filters}
        ORDER BY f.rank, f.id
    """
    params: tuple[Any, ...] = kinds + patterns
    if limit is not None:
        sql += " LIMIT ?"
        params += (limit,)
    return memory.rfl.execute(sql, params).fetchall()


def _candidates(memory: _Memory, seed: Track) -> set[int]:
    candidates: set[int] = set()
    track_id = seed["id"]

    def add_key(key: str) -> None:
        candidates.update(memory.keys.get(key, ()))

    for row in _pair_notes(memory, track_id):
        if row["track_b"] is not None:
            candidates.add(row["track_b"])

    facts = _facts(memory, seed["key"])
    artist = " ".join(seed["artist"].split()).casefold()
    writers = {
        name
        for fact in facts if fact["kind"] == "writers"
        for name in _writers(fact["text"])
        if name != artist
    }
    for writer in sorted(writers):
        for row in _fact_candidates(
            memory, ("writers",), ("%" + _like(writer) + "%",), 200
        ):
            if writer in _writers(row["text"]):
                add_key(row["song_key"])

    if memory.rfl is not None:
        for fact in facts:
            if fact["kind"] not in {"samples", "cover_of", "covers"}:
                continue
            for straight, curly in _QUOTES.findall(fact["text"]):
                title = straight or curly
                rows = memory.rfl.execute(
                    """
                    SELECT key, artist, title FROM songs
                    WHERE status IN ('ok', 'research')
                      AND title = ? COLLATE NOCASE
                    ORDER BY key LIMIT 200
                    """,
                    (title,),
                )
                for row in rows:
                    target = {"artist": row["artist"] or "", "title": row["title"]}
                    if _mentions_song(fact["text"], target):
                        add_key(row["key"])
        for row in _fact_candidates(
            memory,
            ("samples", "cover_of", "covers"),
            (
                "%" + _like(seed["title"]) + "%",
                "%" + _like(seed["artist"]) + "%",
            ),
            200,
        ):
            if _mentions_song(row["text"], seed):
                add_key(row["song_key"])

    bigrams = {
        bigram
        for fact in facts if fact["kind"] == "theme"
        for bigram in _bigrams(fact["text"], _identity_words(seed))
    }
    for first, second in sorted(bigrams)[:10]:
        pattern = "%" + _like(first) + "%" + _like(second) + "%"
        for row in _fact_candidates(memory, ("theme",), (pattern,), 200):
            add_key(row["song_key"])

    for tag in _tags(memory, track_id):
        if _skip_tag(tag):
            continue
        candidates.update(
            row["track_id"]
            for row in memory.aud.execute(
                "SELECT track_id FROM dj_track_tags WHERE tag = ? COLLATE NOCASE",
                (tag,),
            )
        )

    if memory.buckets is not None:
        for bucket_id, _ in _buckets(memory, track_id):
            candidates.update(
                row["track_id"]
                for row in memory.buckets.execute(
                    "SELECT track_id FROM bucket_tracks WHERE bucket_id = ?",
                    (bucket_id,),
                )
            )

    release = next(
        (fact for fact in facts
         if fact["kind"] == "release" and _year(fact["text"])),
        None,
    )
    if release is not None:
        year = _year(release["text"])
        for row in _fact_candidates(memory, ("release",), (f"%{year}%",), 200):
            if _year(row["text"]) == year:
                add_key(row["song_key"])

    codes = _compatible_codes(seed["musical_key"])
    if codes and seed["bpm"] is not None and seed["bpm"] > 0:
        marks = ",".join("?" for _ in codes)
        ranges = []
        for factor in (0.5, 1.0, 2.0):
            ranges.extend((
                seed["bpm"] * 0.94 / factor,
                seed["bpm"] * 1.06 / factor,
            ))
        candidates.update(
            row["id"]
            for row in memory.aud.execute(
                f"""
                SELECT id FROM tracks
                WHERE (content_kind = 'music' OR content_kind IS NULL)
                  AND UPPER(TRIM(musical_key)) IN ({marks})
                  AND (bpm BETWEEN ? AND ? OR bpm BETWEEN ? AND ?
                       OR bpm BETWEEN ? AND ?)
                ORDER BY id LIMIT 200
                """,
                codes + tuple(ranges),
            )
        )
    return candidates


def _timestamp(value: Any) -> float | None:
    try:
        if isinstance(value, datetime):
            date = value
        else:
            try:
                return float(value)
            except (TypeError, ValueError):
                date = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _recent(
    memory: _Memory, candidates: set[int], now: datetime | float | str | None
) -> set[int]:
    instant = (
        datetime.now(timezone.utc).timestamp() if now is None else _timestamp(now)
    )
    if instant is None:
        raise ValueError("Unparseable now")
    cutoff = instant - COOLDOWN_HOURS * 3600
    candidate_keys = {
        memory.tracks[tid]["key"] for tid in candidates if tid in memory.tracks
    }
    ids = sorted({
        tid for key in candidate_keys for tid in memory.keys.get(key, ())
    })
    recent_keys = set()
    for offset in range(0, len(ids), 400):
        chunk = ids[offset:offset + 400]
        marks = ",".join("?" for _ in chunk)
        for row in memory.aud.execute(
            f"SELECT track_id, timestamp FROM play_stats WHERE track_id IN ({marks})",
            chunk,
        ):
            timestamp = _timestamp(row["timestamp"])
            if timestamp is not None and cutoff <= timestamp <= instant:
                recent_keys.add(memory.tracks[row["track_id"]]["key"])
    return {
        tid for key in recent_keys for tid in memory.keys.get(key, ())
    }


def context(track_id: int) -> dict[str, Any] | None:
    """Return this recording's facts, measurements, tags, and safe pair notes."""
    try:
        with _memory() as memory:
            return _context(memory, track_id)
    except Exception as exc:
        _error("context", exc)
        return None


def link(track_a: int, track_b: int) -> list[Fact]:
    """Return stored evidence for a transition, or a single explicit veto."""
    try:
        with _memory() as memory:
            return _link(memory, track_a, track_b)
    except Exception as exc:
        _error("link", exc)
        return []


def after(
    track_id: int,
    n: int = 8,
    exclude_ids: Iterable[int] = (),
    now: datetime | float | str | None = None,
) -> list[dict[str, Any]]:
    """Rank candidates reached through evidence rather than scoring the library."""
    try:
        if n <= 0:
            return []
        with _memory() as memory:
            seed = memory.tracks.get(track_id)
            if seed is None:
                return []
            candidates = _candidates(memory, seed)
            excluded = set(exclude_ids) | _banned(memory)
            excluded.update(_recent(memory, candidates, now))
            seen_keys: set[str] = set()
            results = []
            for candidate_id in sorted(candidates):
                candidate = memory.tracks.get(candidate_id)
                if (
                    candidate is None
                    or candidate_id == track_id
                    or candidate_id in excluded
                    or candidate["key"] == seed["key"]
                    or candidate["key"] in seen_keys
                ):
                    continue
                reasons = _link(memory, track_id, candidate_id)
                kinds = {reason["kind"] for reason in reasons}
                if "veto" in kinds:
                    continue
                score = sum(WEIGHTS.get(kind, 0) for kind in kinds)
                if score == 0 or kinds <= WEAK_KINDS:
                    continue
                seen_keys.add(candidate["key"])
                results.append({
                    "track_id": candidate_id,
                    "title": candidate["title"],
                    "artist": candidate["artist"],
                    "score": score,
                    "reasons": reasons,
                })
            results.sort(key=lambda item: (-item["score"], item["track_id"]))
            return results[:n]
    except Exception as exc:
        _error("after", exc)
        return []


def theme(query: str, n: int = 25) -> list[dict[str, Any]]:
    """Find tagged tracks, named buckets, then whole-phrase sourced facts."""
    try:
        query = query.strip()
        if len(query) < 3 or n <= 0:
            return []
        with _memory() as memory:
            banned = _banned(memory)
            results: list[dict[str, Any]] = []
            seen: set[str] = set()  # one entry per song, not per copy

            def add(track_id: int, why: Fact) -> None:
                track = memory.tracks.get(track_id)
                if (
                    track is None or track_id in banned
                    or track["key"] in seen or len(results) >= n
                ):
                    return
                seen.add(track["key"])
                results.append({
                    "track_id": track_id,
                    "title": track["title"],
                    "artist": track["artist"],
                    "why": why,
                })

            if not _skip_tag(query):
                for row in memory.aud.execute(
                    """
                    SELECT track_id, tag FROM dj_track_tags
                    WHERE tag = ? COLLATE NOCASE ORDER BY track_id, tag
                    """,
                    (query,),
                ):
                    if not _skip_tag(row["tag"]):
                        add(row["track_id"], _reason(
                            "tag", row["tag"], "Todd's tags"
                        ))
            if len(results) >= n:
                return results

            if memory.buckets is not None:
                rows = memory.buckets.execute(
                    """
                    SELECT b.name, t.track_id
                    FROM buckets b JOIN bucket_tracks t ON t.bucket_id = b.id
                    ORDER BY b.name, b.id, t.position, t.track_id
                    """
                )
                for row in rows:
                    if query.casefold() in row["name"].casefold():
                        add(row["track_id"], _reason(
                            "bucket", row["name"], "DJ buckets"
                        ))
            if len(results) >= n:
                return results

            pattern = re.compile(r"\b" + re.escape(query) + r"\b", re.I)
            rows = _fact_candidates(
                memory,
                ("theme", "summary", "history", "recording"),
                ("%" + _like(query) + "%",),
                500,
            )
            for row in rows:
                if pattern.search(row["text"]):
                    why = _row_fact(row)
                    for track_id in memory.keys.get(row["song_key"], ()):
                        add(track_id, why)
            return results
    except Exception as exc:
        _error("theme", exc)
        return []


def queue_context(
    track_ids: Iterable[int], max_tracks: int = 5, max_facts: int = 2
) -> str:
    """Format a compact sourced block for appending to a queue result."""
    try:
        if max_tracks <= 0:
            return ""
        ids = []
        for track_id in track_ids:
            ids.append(track_id)
            if len(ids) >= max_tracks:
                break
        if not ids:
            return ""
        with _memory() as memory:
            contexts = [_context(memory, track_id) for track_id in ids]
            lines = ["CONTEXT (sourced; weave in, never add to it):"]
            has_evidence = False
            for item in contexts:
                if item is None:
                    continue
                facts = item["facts"][:max(0, max_facts)]
                has_evidence = has_evidence or bool(facts)
                prose = "; ".join(
                    f"{fact['text']} ({fact['source']})" for fact in facts
                )
                line = f"- {item['title']} - {item['artist']}:"
                if prose:
                    line += " " + prose
                lines.append(line)

            for index in range(len(ids) - 1):
                a, b = contexts[index], contexts[index + 1]
                if a is None or b is None:
                    continue
                reasons = _link(memory, ids[index], ids[index + 1])
                if not reasons:
                    continue
                has_evidence = True
                veto = next(
                    (reason for reason in reasons if reason["kind"] == "veto"),
                    None,
                )
                if veto is not None:
                    lines.append(
                        f"  CAUTION {a['title']} -> {b['title']}: {veto['text']}"
                    )
                else:
                    reason = reasons[0]
                    lines.append(
                        f"  {a['title']} -> {b['title']}: "
                        f"{reason['text']} ({reason['source']})"
                    )
            if not has_evidence:
                return ""
            block = ""
            for line in lines:
                addition = "\n" + line
                if len(block) + len(addition) > 1200:
                    break
                block += addition
            return block
    except Exception as exc:
        _error("queue_context", exc)
        return ""
