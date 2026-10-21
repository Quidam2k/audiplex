"""Library scanner — walks directories, extracts metadata, upserts to DB."""

import logging
import os
from pathlib import Path

from sqlalchemy.orm import Session

from audiplex.config import LibraryRoot
from audiplex.cue_parser import parse_cue_file
from audiplex.libation import LibationBook, asin_from_folder
from audiplex.libation import load_index as load_libation_index
from audiplex.models import Album, Artist, Book, Chapter, PlaybackPosition, Track
from audiplex.scanners.audiobook_misc import _process_book_folder
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
LIBATION_FOLDER_EXTENSIONS = {".mp3"}  # #8707 only ever scanned as a folder book


def _apply_libation(book: Book, lb: LibationBook | None) -> None:
    """#8707: a folder book's tags describe chapters, so Libation's own record wins."""
    if lb is None:
        return
    book.title = lb.title or book.title
    book.author = lb.authors or book.author
    book.narrator = lb.narrators or book.narrator
    if lb.series:
        book.series, book.series_sequence = lb.series, lb.series_sequence
        book.series_source = "libation"


def _move_positions_to_folder_book(db: Session, book: Book) -> int:
    """#8707: carry listening positions from old per-chapter-file rows onto the
    folder book before the sweep cascades them away. Offset = that file's chapter
    start + position within it. Newest position per user wins."""
    starts = {
        _normcase(c.file_path): c for c in db.query(Chapter).filter(Chapter.book_id == book.id)
        if c.file_path
    }
    folder = _normcase(book.file_path)
    moved = 0
    old_rows = [
        b for b in db.query(Book).filter(Book.id != book.id, Book.file_path.like(book.file_path + "%"))
        if os.path.dirname(_normcase(b.file_path)) == folder
    ]
    for old in old_rows:
        chapter = starts.get(_normcase(old.file_path))
        for pos in db.query(PlaybackPosition).filter(PlaybackPosition.book_id == old.id).all():
            if chapter is None:
                continue
            mine = db.query(PlaybackPosition).filter(
                PlaybackPosition.book_id == book.id, PlaybackPosition.user_id == pos.user_id
            ).first()
            if mine and mine.updated_at >= pos.updated_at:
                continue
            if mine:
                db.delete(mine)
                db.flush()
            pos.book_id = book.id
            pos.position_seconds = chapter.start_seconds + pos.position_seconds
            pos.chapter_index = chapter.index
            moved += 1
    if moved:
        logger.info("Moved %d playback position(s) onto folder book %s", moved, book.file_path)
    return moved


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

    # #8707: Libation's SplitFilesByChapter writes one file per chapter, and
    # podcast-style titles arrive as .mp3. Any folder that is not exactly one
    # .m4b/.m4a is one multi-track book (audiobook_misc shape: chapter per file),
    # not a "book" per file.
    by_folder: dict[Path, list[Path]] = {}
    for ext in AUDIO_EXTENSIONS | LIBATION_FOLDER_EXTENSIONS:
        for audio_file in lib_path.rglob(f"*{ext}"):
            by_folder.setdefault(audio_file.parent, []).append(audio_file)
    single_files: list[Path] = []
    libation = None
    for folder, files in by_folder.items():
        # Loose files in the root itself are separate books, never chapters.
        if folder == lib_path:
            single_files += [f for f in files if f.suffix.lower() in AUDIO_EXTENSIONS]
            continue
        if len(files) == 1 and files[0].suffix.lower() in AUDIO_EXTENSIONS:
            single_files.append(files[0])
            continue
        if libation is None:
            libation = load_libation_index()
        found_paths.add(str(folder))
        try:
            action, warnings = _process_book_folder(db, folder, cover_cache_dir)
            errors.extend(warnings)
            book = db.query(Book).filter(Book.file_path == str(folder)).first()
            if book:
                _apply_libation(book, libation.get(asin_from_folder(folder.name) or ""))
                _move_positions_to_folder_book(db, book)
            if action == "added":
                added += 1
            elif action == "updated":
                updated += 1
        except Exception as e:
            errors.append(f"Error processing {folder}: {e}")
            logger.error(f"Error processing {folder}", exc_info=True)

    for audio_file in single_files:
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


def _root_is_readable(path: str) -> bool:
    """True only if the root is BOTH reachable and enumerable (#842).

    os.path.isdir alone is not enough: an unplugged drive letter or a
    disconnected SMB share can resolve far enough to look like a directory and
    then raise on the first read. We only ever sweep entries under a root that
    actually listed, so we must prove the listing works, not just that the path
    exists. An empty-but-readable directory is legitimately readable.
    """
    try:
        if not os.path.isdir(path):
            return False
        with os.scandir(path) as it:
            next(it, None)
        return True
    except OSError:
        return False


def _normcase(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _under_any(path: str, roots: list[str]) -> bool:
    """Is `path` inside one of `roots`? Case/separator-insensitive."""
    target = _normcase(path)
    for root in roots:
        r = _normcase(root)
        if target == r or target.startswith(r + os.sep):
            return True
    return False


def scan_library(db: Session, library_roots, cover_cache_dir: str) -> ScanResultSchema:
    """Scan all library roots and update the database.

    Each scanner returns its own (result, found_paths). The orchestrator
    unions the found-path sets across roots before sweeping books whose
    files no longer exist anywhere — otherwise one root's scan would
    delete the other root's books.

    #842 — the sweeps are scoped to roots that were READABLE this pass, not
    merely configured. Previously the guard was "a root of this category was
    iterated", which is true even when the scanner bailed out because the drive
    was missing: the found-set then comes back empty and every row under that
    root looks deleted. That is the 598-audiobook hazard. An entry is now only
    ever swept if it lives under a root we successfully enumerated this pass,
    so an unplugged drive or a dropped share makes its entries INVISIBLE, not
    deleted, and they return when the root does. The trade — deliberate — is
    that rows under a root removed from config are no longer garbage-collected
    by a scan; they have to be removed on purpose.
    """
    roots = _normalize_roots(library_roots)
    added = 0
    updated = 0
    errors: list[str] = []
    book_found_paths: set[str] = set()
    album_found_paths: set[str] = set()
    # Roots proven readable THIS pass — the only ones whose entries may be
    # swept. Empty list => that family's sweep is skipped entirely.
    readable_book_roots: list[str] = []
    readable_album_roots: list[str] = []

    for root in roots:
        if not _root_is_readable(root.path):
            msg = (
                f"Library root unreadable, skipping (its existing entries are "
                f"left untouched): {root.path}"
            )
            errors.append(msg)
            logger.warning("%s", msg)
            continue

        if root.category == "audiobook_clean":
            partial, partial_paths = _scan_libation(db, root.path, cover_cache_dir)
            book_found_paths |= partial_paths
            readable_book_roots.append(root.path)
        elif root.category == "audiobook_misc":
            from audiplex.scanners.audiobook_misc import scan_misc
            partial, partial_paths = scan_misc(db, root.path, cover_cache_dir)
            book_found_paths |= partial_paths
            readable_book_roots.append(root.path)
        elif root.category == "meditation":
            # #839 — meditations are Book rows like the audiobook scanners, so
            # they feed the same found-paths sweep.
            from audiplex.scanners.meditation import scan_meditation
            partial, partial_paths = scan_meditation(db, root.path, cover_cache_dir)
            book_found_paths |= partial_paths
            readable_book_roots.append(root.path)
        elif root.category == "music":
            from audiplex.scanners.music import scan_music
            partial, partial_paths = scan_music(
                db, root.path, cover_cache_dir, exclude=root.exclude  # #5448
            )
            from audiplex.scanners.music import apply_content_kinds  # #ride0928
            apply_content_kinds(db, root.path, root.content_kind)
            album_found_paths |= partial_paths
            readable_album_roots.append(root.path)
        else:
            errors.append(f"Unknown category: {root.category}")
            continue

        added += partial.added
        updated += partial.updated
        errors.extend(partial.errors)

    removed = 0

    # Sweep books no longer present in any audiobook root that we could
    # actually read this pass (#842). A book under an unreadable — or no
    # longer configured — root is left alone: absence of evidence that the
    # file is gone is not evidence of deletion.
    if readable_book_roots:
        try:
            for book in db.query(Book).all():
                if book.file_path in book_found_paths:
                    continue
                if not _under_any(book.file_path, readable_book_roots):
                    continue
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

    # Sweep albums (and cascade tracks) no longer on disk, scoped the same
    # way (#842): only under music roots we successfully enumerated.
    if readable_album_roots:
        try:
            for album in db.query(Album).all():
                if album.folder_path in album_found_paths:
                    continue
                if not _under_any(album.folder_path, readable_album_roots):
                    continue
                db.delete(album)
                removed += 1
        except Exception as e:
            errors.append(f"Error sweeping removed albums: {e}")
            logger.error("Error sweeping removed albums", exc_info=True)

        # Garbage-collect Artists with no remaining albums AND no remaining
        # tracks. The track clause is not redundant: since #3037 a track can
        # carry its own artist, distinct from the album's. A flat dump of
        # YouTube rips is one "Various Artists" album whose tracks name a
        # hundred real performers, and none of them owns an album. Checking
        # albums alone deleted every one of them, and `Artist.tracks` has no
        # delete cascade, so SQLAlchemy nulled tracks.artist_id on the way out.
        try:
            db.flush()
            orphan_artists = (
                db.query(Artist)
                .filter(~Artist.albums.any(), ~Artist.tracks.any())
                .all()
            )
            for a in orphan_artists:
                db.delete(a)
        except Exception as e:
            errors.append(f"Error garbage-collecting orphan artists: {e}")
            logger.error("Error garbage-collecting orphan artists", exc_info=True)

        # #8707: one library entry per recording; mix-folder copies stay on disk.
        try:
            from audiplex.dedup import mark_duplicates
            db.flush()
            mark_duplicates(db)
        except Exception as e:
            errors.append(f"Error marking duplicates: {e}")
            logger.error("Error marking duplicates", exc_info=True)

    db.commit()

    return ScanResultSchema(added=added, updated=updated, removed=removed, errors=errors)
