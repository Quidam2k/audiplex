"""Scanner for a flat folder of standalone meditation tracks.

Unlike the audiobook_misc scanner — which collapses a folder of audio
files into ONE book with N chapters — this treats every audio file
directly in the root as its own Book (category "meditation"). That
matters for delivery: a meditation is a standalone ~10 minute session,
and one-Book-per-file is what makes each one individually streamable
via the whole-file `/api/stream/{book_id}` endpoint and therefore
individually downloadable by the Android client.

Files are never rewritten or transcoded (#648) — mutagen is only ever
opened for reading, and audio reaches the client byte-for-byte through
the normal range-request path.
"""

import logging
import re
from pathlib import Path

import mutagen
from sqlalchemy.orm import Session

from audiplex.models import Book, Chapter
from audiplex.schemas import ScanResultSchema
from audiplex.utils.cover_art import extract_embedded_cover, save_cover
from audiplex.utils.metadata import compute_file_hash, normalize_author

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".m4a", ".m4b", ".mp3"}


def _easy_tag(audio, key: str) -> str | None:
    """Pull a single string tag from a mutagen File(easy=True) object."""
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


def _title_from_stem(stem: str) -> str:
    """Clean a filename stem into a display title.

    Meditation filenames use "_" where the source title had a "|" (a
    character Windows forbids), so a stem reads "Daily Calm _ 10 Minute
    Mindfulness Meditation" and "The _Feel Good_ Reset".

    Leading digits are only stripped when a separator marks them as track
    numbering ("03 - Foo"). A bare leading number is content here — nearly
    every one of these titles opens with "10 Minute ...", and stripping it
    would mangle the title.
    """
    cleaned = re.sub(r"^\s*\d{1,3}\s*[-._)]\s+", "", stem).strip() or stem
    cleaned = cleaned.replace(" _ ", " - ")  # "|" separator in the source title
    cleaned = cleaned.replace("_", " ")  # leftovers used as quote marks
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _read_file(path: Path) -> dict:
    """Open one audio file and return duration + a usable title/author.

    The title comes from the FILENAME, not the title tag. That inverts the
    usual precedence deliberately: meditation files arrive from a downloader
    that names them by the source title, and their embedded tags are junk
    inherited from unrelated media. In the shipped set of ten, eight files
    carry no tags at all and the other two claim titles of "|" and
    "Feel Good" with artists "N" and "A" — trusting those would name a book
    "|" and throw away the real title sitting in the filename.

    Author still comes from tags when one looks substantive, so a properly
    tagged file added later still gets credited.
    """
    audio = mutagen.File(str(path), easy=True)
    if audio is None:
        raise ValueError(f"mutagen could not parse {path}")

    duration = float(audio.info.length) if audio.info and audio.info.length else 0.0
    author = _easy_tag(audio, "albumartist") or _easy_tag(audio, "artist")
    if author and len(author.strip()) < 3:
        author = None  # single-letter junk ("N", "A"), not a real credit
    return {"duration": duration, "title": _title_from_stem(path.stem), "author": author}


def _audio_files(root: Path) -> list[Path]:
    """Audio files directly in `root` (not recursive), in natural order."""
    try:
        found = [
            e for e in root.iterdir()
            if e.is_file() and e.suffix.lower() in AUDIO_EXTENSIONS
        ]
    except (OSError, PermissionError):
        return []
    return sorted(found, key=lambda p: p.name.lower())


def _process_file(db: Session, path: Path, cover_cache_dir: str) -> str:
    """Upsert one meditation file as its own Book. Returns the action taken."""
    file_path_str = str(path)
    file_hash = compute_file_hash(file_path_str)

    existing = db.query(Book).filter(Book.file_path == file_path_str).first()
    if existing and existing.file_hash == file_hash:
        return "skipped"

    info = _read_file(path)
    title = info["title"]
    author = normalize_author(info["author"])
    duration = info["duration"]
    file_size = path.stat().st_size

    if existing:
        book = existing
        book.title = title
        book.author = author
        book.narrator = None
        book.series = None
        book.series_sequence = None
        book.duration_seconds = duration
        book.file_size = file_size
        book.file_hash = file_hash
        book.cue_path = None
        book.category = "meditation"
        db.query(Chapter).filter(Chapter.book_id == book.id).delete()
        action = "updated"
    else:
        book = Book(
            title=title,
            author=author,
            category="meditation",
            duration_seconds=duration,
            file_path=file_path_str,
            cue_path=None,
            has_cover=False,
            file_size=file_size,
            file_hash=file_hash,
        )
        db.add(book)
        db.flush()
        action = "added"

    # One chapter spanning the file. file_path stays None so the client
    # streams it whole via /api/stream/{book_id} rather than per-track.
    db.add(Chapter(
        book_id=book.id,
        index=0,
        title=title,
        start_seconds=0.0,
        end_seconds=duration,
        file_path=None,
    ))

    # extract_embedded_cover sniffs the container, so this stays correct for
    # .mp3 sources too — extract_cover_art() would assume MP4 and raise.
    embedded = extract_embedded_cover(path)
    if embedded:
        data, ext = embedded
        save_cover(str(book.id), data, ext, cover_cache_dir)
    book.has_cover = embedded is not None

    return action


def scan_meditation(
    db: Session, lib_path_str: str, cover_cache_dir: str
) -> tuple[ScanResultSchema, set[str]]:
    """Scan a flat meditation root — one Book per audio file."""
    added = 0
    updated = 0
    errors: list[str] = []
    found_paths: set[str] = set()

    lib_path = Path(lib_path_str)
    if not lib_path.exists():
        errors.append(f"Library path does not exist: {lib_path_str}")
        return ScanResultSchema(added=0, updated=0, removed=0, errors=errors), found_paths

    for path in _audio_files(lib_path):
        found_paths.add(str(path))
        try:
            action = _process_file(db, path, cover_cache_dir)
            if action == "added":
                added += 1
            elif action == "updated":
                updated += 1
        except Exception as e:
            errors.append(f"Error processing {path}: {e}")
            logger.error(f"Error processing {path}", exc_info=True)

    return (
        ScanResultSchema(added=added, updated=updated, removed=0, errors=errors),
        found_paths,
    )
