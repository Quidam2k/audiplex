"""Music scanner — walks a music root and populates Artist / Album / Track rows.

Album identity = deepest folder containing audio files. Multi-disc folders
(`CD1`, `cd 1`, `-cd 2`, `Disc 2`, etc.) are merged into the parent album.
Metadata is path-first: `<root>/Artists & Albums/<Genre>/<Artist>/<Album>/`
yields genre + artist + title from the path; tags are enrichment for year
and per-track titles. Loose top-level folders become "Various Artists"
albums.

WMA files are skipped (Media3 can't decode them natively).
"""

import hashlib
import logging
import os
import re
from collections import Counter
from pathlib import Path

import mutagen
from sqlalchemy.orm import Session

from audiplex import tag_repair
from audiplex.models import Album, Artist, Track, TrackTagRepair
from audiplex.schemas import ScanResultSchema
from audiplex.utils.cover_art import (
    extract_embedded_cover,
    find_filesystem_cover,
    save_cover,
)

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".wav", ".mpeg", ".mp2"}
SKIPPED_EXTENSIONS = {".wma"}

ALBUM_COVER_NAMES = (
    "cover.jpg", "cover.jpeg", "cover.png",
    "Cover.jpg", "Cover.jpeg", "Cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "Folder.jpg", "Folder.jpeg", "Folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "Front.jpg", "Front.jpeg", "Front.png",
)

DISC_FOLDER_RE = re.compile(r"^[\s\-_]*(cd|disc)[\s_]*(\d+)\b", re.IGNORECASE)
LEADING_NUM_RE = re.compile(r"^\s*(\d+)")
GENRE_PARENT = "Artists & Albums"
VARIOUS_ARTISTS = "Various Artists"


def _natural_key(name: str) -> list:
    parts = re.split(r"(\d+)", name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def _direct_audio_files(folder: Path) -> list[Path]:
    """Return audio files directly in `folder` (not recursive). Excludes WMA."""
    out = []
    try:
        for entry in folder.iterdir():
            if entry.is_file():
                ext = entry.suffix.lower()
                if ext in AUDIO_EXTENSIONS:
                    out.append(entry)
    except (OSError, PermissionError):
        return []
    return out


def _count_skipped_wma(folder: Path) -> int:
    try:
        return sum(
            1 for entry in folder.iterdir()
            if entry.is_file() and entry.suffix.lower() in SKIPPED_EXTENSIONS
        )
    except (OSError, PermissionError):
        return 0


def _is_disc_folder(name: str) -> int | None:
    """If `name` matches a disc-folder pattern, return the disc number."""
    m = DISC_FOLDER_RE.match(name)
    return int(m.group(2)) if m else None


def _easy_tag(audio, key: str):
    try:
        val = audio.get(key)
    except Exception:
        return None
    if not val:
        return None
    first = val[0]
    if isinstance(first, str):
        first = first.strip()
    return first or None


def _read_track(track_path: Path) -> dict | None:
    """Open a track and pull duration + tags; return None on failure."""
    try:
        audio = mutagen.File(str(track_path), easy=True)
    except Exception:
        return None
    if audio is None:
        return None

    duration = float(audio.info.length) if audio.info and audio.info.length else 0.0
    title = _easy_tag(audio, "title")
    # The artist tag is READ but never trusted blindly — on a YouTube rip it
    # holds the uploading channel, not the performer. tag_repair.propose weighs
    # it against the title. Before #3037 it was not read at all, which is why a
    # correctly tagged album landed under "Various Artists".
    artist = _easy_tag(audio, "artist")
    comment = _easy_tag(audio, "comment")
    year_tag = _easy_tag(audio, "date") or _easy_tag(audio, "year")
    year = None
    if year_tag:
        m = re.search(r"\d{4}", str(year_tag))
        if m:
            try:
                year = int(m.group(0))
            except ValueError:
                year = None
    track_num_tag = _easy_tag(audio, "tracknumber")
    track_num = None
    if track_num_tag:
        m = re.match(r"\s*(\d+)", str(track_num_tag))
        if m:
            try:
                track_num = int(m.group(1))
            except ValueError:
                track_num = None

    return {
        "duration": duration,
        "title": title,
        "artist": artist,
        "comment": comment,
        "year": year,
        "track_number": track_num,
    }


def _title_fallback(track_path: Path) -> str:
    stem = track_path.stem
    cleaned = re.sub(r"^[\s\-_\d.]+", "", stem).strip()
    return cleaned or stem


def _track_number_from_filename(track_path: Path) -> int | None:
    m = LEADING_NUM_RE.match(track_path.name)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _folder_hash(folder_path: str, total_duration: float, track_count: int) -> str:
    payload = f"{folder_path}:{total_duration:.3f}:{track_count}".encode()
    return hashlib.md5(payload).hexdigest()


def _genre_from_path(album_folder: Path, root: Path) -> str | None:
    """Return the genre from `<root>/Artists & Albums/<Genre>/...`, else None."""
    try:
        rel = album_folder.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == GENRE_PARENT:
        return parts[1]
    return None


def _artist_from_path(album_folder: Path, root: Path) -> str:
    """Artist = album folder's parent unless album_folder is directly under root.

    The parent's NAME can be empty — when the album folder is the music root
    itself (`q:\\music`), its parent is the drive root, whose `.name` is `""`.
    That produced one nameless artist owning 207 loose tracks (#3037). A folder
    that is its own album has no artist above it to inherit, so it is Various
    Artists, and each track's real artist is resolved per file instead.
    """
    if album_folder == root or album_folder.parent == root:
        return VARIOUS_ARTISTS
    return album_folder.parent.name or VARIOUS_ARTISTS


def _get_or_create_artist(db: Session, name: str, cache: dict[str, Artist]) -> Artist:
    if name in cache:
        return cache[name]
    artist = db.query(Artist).filter(Artist.name == name).first()
    if artist is None:
        artist = Artist(name=name)
        db.add(artist)
        db.flush()
    cache[name] = artist
    return artist


def _build_track_list(album_folder: Path) -> list[tuple[Path, int]]:
    """Return [(track_path, disc_number), ...] for this album.

    If the album folder has direct audio files, those are disc 1.
    Otherwise look for disc-named subfolders (CD1/Disc 2/-cd 1/...) and
    merge their tracks under the parent.
    """
    direct = _direct_audio_files(album_folder)
    if direct:
        return [(t, 1) for t in direct]

    out: list[tuple[Path, int]] = []
    try:
        subdirs = [d for d in album_folder.iterdir() if d.is_dir()]
    except (OSError, PermissionError):
        return []

    for sub in sorted(subdirs, key=lambda p: _natural_key(p.name)):
        disc = _is_disc_folder(sub.name)
        if disc is None:
            continue
        for t in _direct_audio_files(sub):
            out.append((t, disc))
    return out


def _walk_album_folders(root: Path):
    """Yield album folders. An album = folder whose tracks live directly in
    it OR whose only audio content is in disc subfolders.

    Walks `os.walk` once; uses topdown=True so we can prune disc subfolders
    out of the recursion when we've claimed them as part of a parent album.
    """
    skipped_disc_dirs: set[str] = set()

    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)

        if str(current_path) in skipped_disc_dirs:
            # We'll still descend (in case of weird layouts), but parent
            # already claimed our tracks.
            continue

        direct_audio = any(
            Path(f).suffix.lower() in AUDIO_EXTENSIONS for f in filenames
        )

        if direct_audio:
            yield current_path
            continue

        # No direct audio — check if this is a parent of disc folders.
        disc_subs = [d for d in dirnames if _is_disc_folder(d) is not None]
        if not disc_subs:
            continue

        # Confirm those disc subfolders actually contain audio. If at
        # least one does, claim them and yield self as album.
        any_audio = False
        for d in disc_subs:
            sub_path = current_path / d
            if _direct_audio_files(sub_path):
                any_audio = True
                skipped_disc_dirs.add(str(sub_path))
        if any_audio:
            yield current_path


def _populate_cover(album: Album, album_folder: Path, tracks: list[Path], cache_dir: str) -> bool:
    """Sidecar priority, then embedded fallback. Returns True if saved."""
    sidecar = find_filesystem_cover(album_folder, ALBUM_COVER_NAMES)
    if sidecar:
        ext = "png" if sidecar.suffix.lower() == ".png" else "jpg"
        save_cover(f"album_{album.id}", sidecar.read_bytes(), ext, cache_dir)
        return True

    for t in tracks:
        embedded = extract_embedded_cover(t)
        if embedded:
            data, ext = embedded
            save_cover(f"album_{album.id}", data, ext, cache_dir)
            return True

    return False


def _repairs_for(db: Session, paths: list[str]) -> dict[str, TrackTagRepair]:
    """Existing repair rows for these files, keyed by path.

    Chunked because SQLite caps the number of bound variables in one statement
    and a flat dump album can hold every track in the library.
    """
    found: dict[str, TrackTagRepair] = {}
    chunk = 500
    for start in range(0, len(paths), chunk):
        rows = (
            db.query(TrackTagRepair)
            .filter(TrackTagRepair.file_path.in_(paths[start : start + chunk]))
            .all()
        )
        for row in rows:
            found[row.file_path] = row
    return found


def _all_weighed(db: Session, track_infos: list[tuple[Path, int, dict]]) -> bool:
    """True when every file in this album already has a repair verdict."""
    paths = [str(tpath) for tpath, _, _ in track_infos]
    return len(_repairs_for(db, paths)) == len(set(paths))


def _propose_for(tpath: Path, info: dict) -> tag_repair.Proposal:
    return tag_repair.propose(
        title_tag=info.get("title"),
        artist_tag=info.get("artist"),
        filename_stem=tpath.stem,
        comment=info.get("comment"),
    )


def _folder_consensus(proposals: list[tag_repair.Proposal]) -> str | None:
    """The artist this folder unanimously agrees on, if it agrees at all.

    Only counts the files confident enough to be applied, and only answers when
    every one of them names the SAME artist. An album with eight tagged tracks
    and two tagless ones is a real album with two gaps; a flat dump of a hundred
    different performers is not unanimous about anything, so this stays silent
    there — the guard falls out of the data instead of needing a special case.
    """
    named = {p.artist for p in proposals if p.auto_applicable and p.artist}
    return next(iter(named)) if len(named) == 1 else None


def _record_proposal(
    db: Session,
    tpath: Path,
    info: dict,
    proposal: tag_repair.Proposal,
    consensus: str | None = None,
) -> TrackTagRepair:
    """Write one file's proposal to the overlay. This is the ingest half of
    #3037: anything that arrives later is weighed by the same ladder that
    repaired the existing catalog, so the library cannot drift back.

    Only `high` confidence is marked applied. Everything else lands as
    pending_review with its evidence attached — a worklist, not a silent gap.
    """
    comment = info.get("comment") or ""
    artist = proposal.artist
    confidence = proposal.confidence
    source = tag_repair.SOURCE_PARSER
    evidence = proposal.evidence
    applied = proposal.auto_applicable

    if consensus and not artist:
        # Fills a blank, never overrides a claim. A file that named somebody —
        # even unconvincingly — keeps its own answer and stays in the review
        # queue; only the ones with nothing to say inherit the folder's.
        artist = consensus
        confidence = tag_repair.HIGH
        source = tag_repair.SOURCE_CONSENSUS
        evidence = (
            f"{proposal.evidence}; every other file in this folder that could "
            f"be identified names {consensus!r}, so this one inherits it"
        )
        applied = bool(proposal.title)

    row = TrackTagRepair(
        file_path=str(tpath),
        source_url=comment if tag_repair.is_youtube_rip(comment) else None,
        proposed_artist=artist,
        proposed_title=proposal.title,
        confidence=confidence,
        source=source,
        evidence=evidence,
        status=tag_repair.APPLIED if applied else tag_repair.PENDING_REVIEW,
    )
    db.add(row)
    return row


def _resolve_track(
    tpath: Path, info: dict, repair: TrackTagRepair | None, album_artist: str
) -> tuple[str, str]:
    """(artist, title) for one file.

    An applied repair wins — it is the only source that has been weighed
    against every signal. Otherwise fall back to the album's path-derived
    artist, which is right for a real `<Artist>/<Album>/` tree and merely
    unhelpful for a flat dump.
    """
    if repair is not None and repair.status == tag_repair.APPLIED:
        artist = repair.proposed_artist or album_artist
        title = repair.proposed_title or info.get("title") or _title_fallback(tpath)
        return artist, title
    return album_artist, (info.get("title") or _title_fallback(tpath))


def _process_album_folder(
    db: Session,
    album_folder: Path,
    root: Path,
    cover_cache_dir: str,
    artist_cache: dict[str, Artist],
) -> tuple[str, list[str]]:
    """Process one album folder; return (action, warnings)."""
    warnings: list[str] = []
    folder_path_str = str(album_folder)

    raw_tracks = _build_track_list(album_folder)
    if not raw_tracks:
        return "skipped", warnings

    raw_tracks.sort(key=lambda pair: (pair[1], _natural_key(pair[0].name)))

    track_infos: list[tuple[Path, int, dict]] = []
    for tpath, disc in raw_tracks:
        info = _read_track(tpath)
        if info is None:
            warnings.append(f"Could not read {tpath}")
            continue
        track_infos.append((tpath, disc, info))

    if not track_infos:
        return "skipped", warnings

    total_duration = sum(info["duration"] for _, _, info in track_infos)
    file_hash = _folder_hash(folder_path_str, total_duration, len(track_infos))

    album_title = album_folder.name
    artist_name = _artist_from_path(album_folder, root)
    genre = _genre_from_path(album_folder, root)
    years = [info["year"] for _, _, info in track_infos if info["year"]]
    year = Counter(years).most_common(1)[0][0] if years else None

    existing = db.query(Album).filter(Album.folder_path == folder_path_str).first()

    if existing:
        existing_hash = _folder_hash(
            existing.folder_path, existing.duration_seconds, existing.track_count
        )
        # The hash covers path, duration and count — nothing about tag quality.
        # An album whose files never change would therefore never be weighed by
        # the repair ladder at all, so the fast path also requires that every
        # file already has a verdict on record (#3037).
        if existing_hash == file_hash and _all_weighed(db, track_infos):
            return "skipped", warnings

    artist = _get_or_create_artist(db, artist_name, artist_cache)

    if existing:
        existing.title = album_title
        existing.artist_id = artist.id
        existing.genre = genre
        existing.year = year
        existing.duration_seconds = total_duration
        existing.track_count = len(track_infos)

        _sync_tracks(db, existing, artist_name, track_infos, artist_cache)
        existing.has_cover = _populate_cover(
            existing, album_folder, [t for t, _, _ in track_infos], cover_cache_dir
        )
        return "updated", warnings

    album = Album(
        title=album_title,
        artist_id=artist.id,
        genre=genre,
        year=year,
        duration_seconds=total_duration,
        track_count=len(track_infos),
        has_cover=False,
        folder_path=folder_path_str,
    )
    db.add(album)
    db.flush()

    _sync_tracks(db, album, artist_name, track_infos, artist_cache)
    album.has_cover = _populate_cover(
        album, album_folder, [t for t, _, _ in track_infos], cover_cache_dir
    )
    return "added", warnings


def _sync_tracks(
    db: Session,
    album: Album,
    album_artist: str,
    track_infos: list[tuple[Path, int, dict]],
    artist_cache: dict[str, Artist],
) -> None:
    """Reconcile this album's tracks against what is on disk, IN PLACE.

    This used to delete every track in a changed album and re-insert it, which
    destroyed listening history on the next scan: PlayStat rows went with the
    ORM delete-orphan cascade, and TrackRating rows were left pointing at dead
    ids that SQLite would later hand to a different song. Dropping one new file
    into the music folder was enough to fire it, because the album's hash is
    (path, duration, track count).

    So: match by file_path — the one identifier that survives a rescan — update
    what changed, and delete only the tracks whose files are genuinely gone.
    """
    existing_by_path = {t.file_path: t for t in album.tracks}
    paths = [str(tpath) for tpath, _, _ in track_infos]
    repairs = _repairs_for(db, paths)

    # Weigh every unverdicted file BEFORE writing any of them down: the folder
    # consensus is a property of the whole album, so it cannot be known while
    # walking one track at a time.
    fresh = {
        str(tpath): _propose_for(tpath, info)
        for tpath, _, info in track_infos
        if str(tpath) not in repairs
    }
    consensus = _folder_consensus(list(fresh.values()))
    for tpath, _, info in track_infos:
        proposal = fresh.get(str(tpath))
        if proposal is not None:
            repairs[str(tpath)] = _record_proposal(db, tpath, info, proposal, consensus)

    seen: set[str] = set()
    for natural_index, (tpath, disc, info) in enumerate(track_infos, start=1):
        path_str = str(tpath)
        seen.add(path_str)

        artist_name, title = _resolve_track(
            tpath, info, repairs.get(path_str), album_artist
        )
        artist = _get_or_create_artist(db, artist_name, artist_cache)

        track_num = (
            _track_number_from_filename(tpath)
            or info.get("track_number")
            or natural_index
        )
        try:
            file_size = tpath.stat().st_size
        except OSError:
            file_size = 0

        track = existing_by_path.get(path_str)
        if track is None:
            db.add(Track(
                title=title,
                album_id=album.id,
                artist_id=artist.id,
                disc_number=disc,
                track_number=track_num,
                duration_seconds=info["duration"],
                file_path=path_str,
                file_size=file_size,
            ))
            continue

        track.title = title
        track.album_id = album.id
        track.artist_id = artist.id
        track.disc_number = disc
        track.track_number = track_num
        track.duration_seconds = info["duration"]
        track.file_size = file_size

    for path_str, track in existing_by_path.items():
        if path_str not in seen:
            db.delete(track)

    db.flush()


def scan_music(
    db: Session, music_root: str, cover_cache_dir: str
) -> tuple[ScanResultSchema, set[str]]:
    """Scan a music library root.

    Returns (ScanResultSchema, set of album folder paths) — the path set
    is used by the orchestrator for the removed-album sweep.
    """
    added = 0
    updated = 0
    errors: list[str] = []
    found_paths: set[str] = set()
    artist_cache: dict[str, Artist] = {}
    skipped_wma = 0

    root = Path(music_root)
    if not root.exists():
        errors.append(f"Music root does not exist: {music_root}")
        return ScanResultSchema(added=0, updated=0, removed=0, errors=errors), found_paths

    for album_folder in _walk_album_folders(root):
        found_paths.add(str(album_folder))
        skipped_wma += _count_skipped_wma(album_folder)
        try:
            action, warnings = _process_album_folder(
                db, album_folder, root, cover_cache_dir, artist_cache
            )
            errors.extend(warnings)
            if action == "added":
                added += 1
            elif action == "updated":
                updated += 1
        except Exception as e:
            errors.append(f"Error processing {album_folder}: {e}")
            logger.error("Error processing %s", album_folder, exc_info=True)

    if skipped_wma:
        logger.info("Skipped %d WMA files (Media3 can't decode)", skipped_wma)

    return (
        ScanResultSchema(added=added, updated=updated, removed=0, errors=errors),
        found_paths,
    )
