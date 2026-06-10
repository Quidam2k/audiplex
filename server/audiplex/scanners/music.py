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

from audiplex.models import Album, Artist, Track
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
    """Artist = album folder's parent unless album_folder is directly under root."""
    if album_folder.parent == root:
        return VARIOUS_ARTISTS
    return album_folder.parent.name


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
        if existing_hash == file_hash:
            return "skipped", warnings

    artist = _get_or_create_artist(db, artist_name, artist_cache)

    if existing:
        existing.title = album_title
        existing.artist_id = artist.id
        existing.genre = genre
        existing.year = year
        existing.duration_seconds = total_duration
        existing.track_count = len(track_infos)

        # Drop existing tracks, re-insert.
        for t in list(existing.tracks):
            db.delete(t)
        db.flush()

        _insert_tracks(db, existing, artist, track_infos)
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

    _insert_tracks(db, album, artist, track_infos)
    album.has_cover = _populate_cover(
        album, album_folder, [t for t, _, _ in track_infos], cover_cache_dir
    )
    return "added", warnings


def _insert_tracks(
    db: Session,
    album: Album,
    artist: Artist,
    track_infos: list[tuple[Path, int, dict]],
) -> None:
    for natural_index, (tpath, disc, info) in enumerate(track_infos, start=1):
        track_num = (
            _track_number_from_filename(tpath)
            or info.get("track_number")
            or natural_index
        )
        title = info.get("title") or _title_fallback(tpath)
        try:
            file_size = tpath.stat().st_size
        except OSError:
            file_size = 0

        db.add(Track(
            title=title,
            album_id=album.id,
            artist_id=artist.id,
            disc_number=disc,
            track_number=track_num,
            duration_seconds=info["duration"],
            file_path=str(tpath),
            file_size=file_size,
        ))


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
