"""Library scanner — walks directories, extracts metadata, upserts to DB."""

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from audiplex.config import LibraryRoot
from audiplex.cue_parser import parse_cue_file
from audiplex.models import Album, Artist, Book, Chapter, Track
from audiplex.schemas import ScanResultSchema
from audiplex.utils.cover_art import extract_cover_art
from audiplex.utils.metadata import (
    apply_override,
    compute_file_hash,
    extract_metadata,
    load_overrides,
    parse_series_from_album,
)

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".m4b", ".m4a"}


def _find_cue_file(audio_path: Path) -> Path | None:
    """Find a companion .cue file with the same stem."""
    cue_path = audio_path.with_suffix(".cue")
    if cue_path.exists():
        return cue_path
    return None


def _build_chapters(audio_path: Path, metadata, duration: float) -> list[dict]:
    """Build chapter list from CUE file, falling back to embedded chapters."""
    cue_path = _find_cue_file(audio_path)
    chapters = []

    if cue_path:
        try:
            cue_chapters = parse_cue_file(str(cue_path))
            for i, ch in enumerate(cue_chapters):
                # Calculate end_seconds from next chapter start or file duration
                end = cue_chapters[i + 1].start_seconds if i + 1 < len(cue_chapters) else duration
                chapters.append({
                    "index": ch.index,
                    "title": ch.title,
                    "start_seconds": ch.start_seconds,
                    "end_seconds": end,
                })
            return chapters
        except Exception as e:
            logger.warning(f"Failed to parse CUE file {cue_path}: {e}")

    # Fallback to embedded chapters
    if metadata.embedded_chapters:
        for i, ch in enumerate(metadata.embedded_chapters):
            end = (
                metadata.embedded_chapters[i + 1]["start_seconds"]
                if i + 1 < len(metadata.embedded_chapters)
                else duration
            )
            chapters.append({
                "index": ch["index"],
                "title": ch["title"],
                "start_seconds": ch["start_seconds"],
                "end_seconds": end,
            })

    return chapters


def _scan_libation(
    db: Session, lib_path_str: str, cover_cache_dir: str
) -> tuple[ScanResultSchema, set[str]]:
    """Scan a Libation-style root: one .m4b/.m4a per book, optional .cue."""
    added = 0
    updated = 0
    errors: list[str] = []
    found_paths: set[str] = set()

    lib_path = Path(lib_path_str)
    if not lib_path.exists():
        errors.append(f"Library path does not exist: {lib_path_str}")
        return ScanResultSchema(added=0, updated=0, removed=0, errors=errors), found_paths

    for ext in AUDIO_EXTENSIONS:
        for audio_file in lib_path.rglob(f"*{ext}"):
            file_path_str = str(audio_file)
            found_paths.add(file_path_str)

            try:
                file_hash = compute_file_hash(file_path_str)

                existing = db.query(Book).filter(Book.file_path == file_path_str).first()

                if existing and existing.file_hash == file_hash:
                    continue

                metadata = extract_metadata(file_path_str)
                cue_path = _find_cue_file(audio_file)
                chapters_data = _build_chapters(audio_file, metadata, metadata.duration_seconds)

                if existing:
                    existing.title = metadata.title
                    existing.author = metadata.author
                    existing.narrator = metadata.narrator
                    existing.series = metadata.series
                    existing.series_sequence = metadata.series_sequence
                    existing.series_raw = metadata.series_raw
                    existing.duration_seconds = metadata.duration_seconds
                    existing.file_size = metadata.file_size
                    existing.file_hash = file_hash
                    existing.has_cover = metadata.has_cover
                    existing.cue_path = str(cue_path) if cue_path else None
                    existing.category = "audiobook_clean"

                    db.query(Chapter).filter(Chapter.book_id == existing.id).delete()
                    for ch in chapters_data:
                        db.add(Chapter(book_id=existing.id, **ch))

                    if metadata.has_cover:
                        extract_cover_art(file_path_str, existing.id, cover_cache_dir)

                    updated += 1
                else:
                    book = Book(
                        title=metadata.title,
                        author=metadata.author,
                        narrator=metadata.narrator,
                        series=metadata.series,
                        series_sequence=metadata.series_sequence,
                        series_raw=metadata.series_raw,
                        category="audiobook_clean",
                        duration_seconds=metadata.duration_seconds,
                        file_path=file_path_str,
                        cue_path=str(cue_path) if cue_path else None,
                        has_cover=metadata.has_cover,
                        file_size=metadata.file_size,
                        file_hash=file_hash,
                    )
                    db.add(book)
                    db.flush()

                    for ch in chapters_data:
                        db.add(Chapter(book_id=book.id, **ch))

                    if metadata.has_cover:
                        extract_cover_art(file_path_str, book.id, cover_cache_dir)

                    added += 1

            except Exception as e:
                errors.append(f"Error processing {audio_file}: {e}")
                logger.error(f"Error processing {audio_file}", exc_info=True)

    return (
        ScanResultSchema(added=added, updated=updated, removed=0, errors=errors),
        found_paths,
    )


def _normalize_roots(library_roots) -> list[LibraryRoot]:
    """Accept LibraryRoot objects or plain strings (legacy)."""
    normalized = []
    for r in library_roots:
        if isinstance(r, LibraryRoot):
            normalized.append(r)
        elif isinstance(r, str):
            normalized.append(LibraryRoot(path=r))
        elif isinstance(r, dict):
            normalized.append(LibraryRoot(**r))
        else:
            raise TypeError(f"Unsupported library root type: {type(r)}")
    return normalized


def scan_library(db: Session, library_roots, cover_cache_dir: str) -> ScanResultSchema:
    """Scan all library roots and update the database.

    Each scanner returns its own (result, found_paths). The orchestrator
    unions the found-path sets across roots before sweeping books whose
    files no longer exist anywhere — otherwise one root's scan would
    delete the other root's books.
    """
    roots = _normalize_roots(library_roots)
    added = 0
    updated = 0
    errors: list[str] = []
    book_found_paths: set[str] = set()
    album_found_paths: set[str] = set()
    has_music_root = False

    for root in roots:
        if root.category == "audiobook_clean":
            partial, partial_paths = _scan_libation(db, root.path, cover_cache_dir)
            book_found_paths |= partial_paths
        elif root.category == "audiobook_misc":
            from audiplex.scanners.audiobook_misc import scan_misc
            partial, partial_paths = scan_misc(db, root.path, cover_cache_dir)
            book_found_paths |= partial_paths
        elif root.category == "music":
            from audiplex.scanners.music import scan_music
            partial, partial_paths = scan_music(db, root.path, cover_cache_dir)
            album_found_paths |= partial_paths
            has_music_root = True
        else:
            errors.append(f"Unknown category: {root.category}")
            continue

        added += partial.added
        updated += partial.updated
        errors.extend(partial.errors)

    removed = 0

    # Sweep books no longer present in any audiobook root.
    try:
        for book in db.query(Book).all():
            if book.file_path not in book_found_paths:
                db.delete(book)
                removed += 1
    except Exception as e:
        errors.append(f"Error sweeping removed books: {e}")
        logger.error("Error sweeping removed books", exc_info=True)

    # Re-derive series/series_sequence from series_raw on every scan.
    # The hash-skip in _scan_libation means existing rows wouldn't otherwise
    # pick up improvements to parse_series_from_album.
    # Skip books where series_source is "openlibrary" or "override" — don't
    # clobber enriched data with null.
    try:
        overrides = load_overrides()
        for book in db.query(Book).filter(Book.category == "audiobook_clean").all():
            if book.series_source in ("openlibrary", "override"):
                continue
            new_series, new_sequence = None, None
            if book.series_raw:
                new_series, new_sequence = parse_series_from_album(book.series_raw)
            if not new_series:
                new_series, new_sequence = apply_override(
                    book.title, book.author, overrides
                )
                if new_series:
                    book.series_source = "override"
            if new_series and new_series != book.series:
                book.series = new_series
                if not book.series_source:
                    book.series_source = "tag"
            if new_sequence and new_sequence != book.series_sequence:
                book.series_sequence = new_sequence
    except Exception as e:
        errors.append(f"Error re-parsing series: {e}")
        logger.error("Error re-parsing series", exc_info=True)

    # Sweep albums (and cascade tracks) no longer on disk. Only when at
    # least one music root was scanned — otherwise we'd nuke the whole
    # music library on an audiobook-only scan.
    if has_music_root:
        try:
            for album in db.query(Album).all():
                if album.folder_path not in album_found_paths:
                    db.delete(album)
                    removed += 1
        except Exception as e:
            errors.append(f"Error sweeping removed albums: {e}")
            logger.error("Error sweeping removed albums", exc_info=True)

        # Garbage-collect Artists with no remaining albums.
        try:
            db.flush()
            orphan_artists = (
                db.query(Artist).filter(~Artist.albums.any()).all()
            )
            for a in orphan_artists:
                db.delete(a)
        except Exception as e:
            errors.append(f"Error garbage-collecting orphan artists: {e}")
            logger.error("Error garbage-collecting orphan artists", exc_info=True)

    db.commit()

    return ScanResultSchema(added=added, updated=updated, removed=removed, errors=errors)
