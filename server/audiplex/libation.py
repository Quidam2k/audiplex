"""Read-only lookup of Libation's own library database (#8707).

Libation names each book folder "<author><title> [<ASIN>]" and its tags are
sometimes thin, so the scanner asks Libation's SQLite DB for the authoritative
title/author/narrator/series keyed by the Audible product id in the folder name.
The DB is opened read-only (Libation may have it open); we never write to it.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB = Path.home() / "Libation" / "LibationContext.db"

_ASIN_SUFFIX = re.compile(r"\[([0-9A-Za-z]{10})\]\s*$")
_ROLE_AUTHOR, _ROLE_NARRATOR = 1, 2


@dataclass(frozen=True)
class LibationBook:
    asin: str
    title: str
    authors: str | None  # names joined with ", " in BookContributor "Order"
    narrators: str | None
    series: str | None  # first series by SeriesBook rowid; None if none
    series_sequence: str | None  # SeriesBook."Order" stripped; None if empty


def asin_from_folder(name: str) -> str | None:
    """"Ginkgo Biloba [B098B9BYKT]" -> "B098B9BYKT" (the trailing bracket only)."""
    match = _ASIN_SUFFIX.search(name)
    return match.group(1).upper() if match else None


def db_path() -> Path:
    override = os.environ.get("AUDIPLEX_LIBATION_DB")
    return Path(override) if override else DEFAULT_DB


def load_index(path: Path | str | None = None) -> dict[str, LibationBook]:
    """ASIN (upper-case) -> LibationBook. Empty dict if the DB is missing/unreadable."""
    db_file = Path(path) if path is not None else db_path()
    if not db_file.exists():
        logger.info("Libation DB not found at %s; skipping Libation lookup", db_file)
        return {}
    try:
        with closing(sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)) as conn:
            books = conn.execute(
                "SELECT BookId, AudibleProductId, Title FROM Books"
            ).fetchall()
            people = conn.execute(
                'SELECT bc.BookId, bc.Role, c.Name FROM BookContributor bc '
                'JOIN Contributors c ON c.ContributorId = bc.ContributorId '
                'WHERE bc.Role IN (1, 2) ORDER BY bc.BookId, bc.Role, bc."Order"'
            ).fetchall()
            series_rows = conn.execute(
                'SELECT sb.BookId, s.Name, sb."Order" FROM SeriesBook sb '
                "JOIN Series s ON s.SeriesId = sb.SeriesId ORDER BY sb.rowid"
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("Could not read Libation DB %s: %s", db_file, exc)
        return {}

    names: dict[tuple[int, int], list[str]] = {}
    for book_id, role, name in people:
        if name:
            names.setdefault((book_id, role), []).append(name)
    series: dict[int, tuple[str, str | None]] = {}
    for book_id, name, order in series_rows:
        series.setdefault(book_id, (name, (order or "").strip() or None))

    index: dict[str, LibationBook] = {}
    for book_id, asin, title in books:
        if not asin:
            continue
        asin = asin.strip().upper()
        if not asin:
            continue
        authors = names.get((book_id, _ROLE_AUTHOR))
        narrators = names.get((book_id, _ROLE_NARRATOR))
        series_name, sequence = series.get(book_id, (None, None))
        index[asin] = LibationBook(
            asin=asin,
            title=title or "",
            authors=", ".join(authors) if authors else None,
            narrators=", ".join(narrators) if narrators else None,
            series=series_name,
            series_sequence=sequence,
        )
    return index
