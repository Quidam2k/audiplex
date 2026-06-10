"""Scanner for non-Libation multi-track audiobook folders.

A book folder is any folder that *directly* contains one or more audio
files. Files are sorted with a natural-sort key (so "01", "2", "10" sort
1/2/10) and stitched into a chapter list with cumulative start times.
The Book row's `file_path` is the folder path (used as identity); each
Chapter's `file_path` is the absolute path to that track. Audio is
streamed per-track via `/api/stream/{book_id}/track/{track_index}`.
"""

import hashlib
import logging
import os
import re
from pathlib import Path

import mutagen
from sqlalchemy.orm import Session

from audiplex.models import Book, Chapter
from audiplex.schemas import ScanResultSchema
from audiplex.utils.cover_art import (
    extract_embedded_cover,
    find_filesystem_cover,
    save_cover,
)
from audiplex.utils.metadata import normalize_author

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".flac", ".ogg"}
COVER_FILENAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.jpeg", "folder.png")


def _natural_key(name: str) -> list:
    """Split into alternating text/integer chunks for natural sort.

    'track01.mp3' < 'track2.mp3' is wrong; this fixes that by treating
    digit runs as integers.
    """
    parts = re.split(r"(\d+)", name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def _easy_tag(audio, key: str) -> str | None:
    """Pull a tag from a mutagen File(easy=True) object."""
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


def _read_track(track_path: Path) -> dict:
    """Open a single audio file and return duration + common tags.

    Falls back to filename stem (with leading numerics stripped) if no
    title tag is present.
    """
    audio = mutagen.File(str(track_path), easy=True)
    if audio is None:
        raise ValueError(f"mutagen could not parse {track_path}")

    duration = float(audio.info.length) if audio.info and audio.info.length else 0.0

    title = (
        _easy_tag(audio, "title")
        or re.sub(r"^[\s\-_\d.]+", "", track_path.stem).strip()
        or track_path.stem
    )
    return {
        "duration": duration,
        "title": title,
        "album": _easy_tag(audio, "album"),
        "artist": _easy_tag(audio, "artist"),
        "albumartist": _easy_tag(audio, "albumartist"),
        "composer": _easy_tag(audio, "composer"),
    }


def _most_common(values: list[str | None]) -> str | None:
    """Return the most common non-None value, or None."""
    counts: dict[str, int] = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _populate_cover(book_id: int, book_folder: Path, tracks: list[Path], cache_dir: str) -> bool:
    """Try embedded picture on first track, then filesystem cover. Returns True if cover saved."""
    if tracks:
        embedded = extract_embedded_cover(tracks[0])
        if embedded:
            data, ext = embedded
            save_cover(str(book_id), data, ext, cache_dir)
            return True

    fs_cover = find_filesystem_cover(book_folder, COVER_FILENAMES)
    if fs_cover:
        ext = "png" if fs_cover.suffix.lower() == ".png" else "jpg"
        save_cover(str(book_id), fs_cover.read_bytes(), ext, cache_dir)
        return True

    return False


def _folder_hash(folder_path: str, total_duration: float, track_count: int) -> str:
    """Cheap identity hash for a misc book — folder path + summary stats."""
    payload = f"{folder_path}:{total_duration:.3f}:{track_count}".encode()
    return hashlib.md5(payload).hexdigest()


def _direct_audio_files(folder: Path) -> list[Path]:
    """Return audio files directly in `folder` (not recursive)."""
    out = []
    try:
        for entry in folder.iterdir():
            if entry.is_file() and entry.suffix.lower() in AUDIO_EXTENSIONS:
                out.append(entry)
    except (OSError, PermissionError):
        return []
    return out


def _walk_book_folders(root: Path):
    """Yield every folder under `root` that directly contains audio files."""
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        # Skip if no audio files directly here
        if any(Path(f).suffix.lower() in AUDIO_EXTENSIONS for f in filenames):
            yield current_path


def _process_book_folder(
    db: Session, book_folder: Path, cover_cache_dir: str
) -> tuple[str, list[str]]:
    """Process a single book folder; return (action, warnings).

    action is one of: "added", "updated", "skipped", "error".
    """
    warnings: list[str] = []
    folder_path_str = str(book_folder)

    tracks = _direct_audio_files(book_folder)
    if not tracks:
        return "skipped", warnings

    tracks.sort(key=lambda p: _natural_key(p.name))

    # Warn if any tracks share an identical sort key — a hint that the
    # collection has filenames human review should look at.
    keys_seen: dict[tuple, str] = {}
    for t in tracks:
        k = tuple(_natural_key(t.name))
        if k in keys_seen:
            warnings.append(
                f"Ambiguous track order in {folder_path_str}: "
                f"{keys_seen[k]} vs {t.name} share sort key"
            )
        else:
            keys_seen[k] = t.name

    track_infos = []
    for t in tracks:
        try:
            track_infos.append((t, _read_track(t)))
        except Exception as e:
            warnings.append(f"Error reading {t}: {e}")

    if not track_infos:
        return "skipped", warnings

    total_duration = sum(info["duration"] for _, info in track_infos)

    book_title = (
        _most_common([info.get("album") for _, info in track_infos])
        or book_folder.name
    )
    author = normalize_author(
        _most_common([info.get("albumartist") for _, info in track_infos])
        or _most_common([info.get("artist") for _, info in track_infos])
        or (book_folder.parent.name if book_folder.parent != book_folder else None)
    )
    narrator = _most_common([info.get("composer") for _, info in track_infos])

    file_hash = _folder_hash(folder_path_str, total_duration, len(track_infos))

    existing = db.query(Book).filter(Book.file_path == folder_path_str).first()
    if existing and existing.file_hash == file_hash:
        return "skipped", warnings

    if existing:
        existing.title = book_title
        existing.author = author
        existing.narrator = narrator
        existing.series = None
        existing.series_sequence = None
        existing.duration_seconds = total_duration
        existing.file_size = sum(t.stat().st_size for t, _ in track_infos)
        existing.file_hash = file_hash
        existing.cue_path = None
        existing.category = "audiobook_misc"

        db.query(Chapter).filter(Chapter.book_id == existing.id).delete()
        cumulative = 0.0
        for i, (t, info) in enumerate(track_infos):
            db.add(Chapter(
                book_id=existing.id,
                index=i,
                title=info["title"],
                start_seconds=cumulative,
                end_seconds=cumulative + info["duration"],
                file_path=str(t),
            ))
            cumulative += info["duration"]

        had_cover = _populate_cover(existing.id, book_folder, [t for t, _ in track_infos], cover_cache_dir)
        existing.has_cover = had_cover
        return "updated", warnings

    book = Book(
        title=book_title,
        author=author,
        narrator=narrator,
        category="audiobook_misc",
        duration_seconds=total_duration,
        file_path=folder_path_str,
        cue_path=None,
        has_cover=False,
        file_size=sum(t.stat().st_size for t, _ in track_infos),
        file_hash=file_hash,
    )
    db.add(book)
    db.flush()

    cumulative = 0.0
    for i, (t, info) in enumerate(track_infos):
        db.add(Chapter(
            book_id=book.id,
            index=i,
            title=info["title"],
            start_seconds=cumulative,
            end_seconds=cumulative + info["duration"],
            file_path=str(t),
        ))
        cumulative += info["duration"]

    book.has_cover = _populate_cover(
        book.id, book_folder, [t for t, _ in track_infos], cover_cache_dir
    )

    return "added", warnings


def scan_misc(
    db: Session, lib_path_str: str, cover_cache_dir: str
) -> tuple[ScanResultSchema, set[str]]:
    """Scan a multi-track audiobook root."""
    added = 0
    updated = 0
    errors: list[str] = []
    found_paths: set[str] = set()

    lib_path = Path(lib_path_str)
    if not lib_path.exists():
        errors.append(f"Library path does not exist: {lib_path_str}")
        return ScanResultSchema(added=0, updated=0, removed=0, errors=errors), found_paths

    for book_folder in _walk_book_folders(lib_path):
        found_paths.add(str(book_folder))
        try:
            action, warnings = _process_book_folder(db, book_folder, cover_cache_dir)
            errors.extend(warnings)
            if action == "added":
                added += 1
            elif action == "updated":
                updated += 1
        except Exception as e:
            errors.append(f"Error processing {book_folder}: {e}")
            logger.error(f"Error processing {book_folder}", exc_info=True)

    return (
        ScanResultSchema(added=added, updated=updated, removed=0, errors=errors),
        found_paths,
    )
