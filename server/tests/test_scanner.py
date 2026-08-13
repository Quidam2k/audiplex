"""Tests for library scanner."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from audiplex.config import LibraryRoot
from audiplex.models import Book, Chapter
from audiplex.scanner import scan_library
from audiplex.utils.metadata import BookMetadata


def _make_fake_metadata(title="Test Book", author="Test Author", **kwargs):
    defaults = dict(
        title=title,
        author=author,
        narrator="Test Narrator",
        series=None,
        series_sequence=None,
        duration_seconds=3600.0,
        file_size=1000,
        file_hash="fakehash",
        has_cover=False,
        embedded_chapters=[],
    )
    defaults.update(kwargs)
    return BookMetadata(**defaults)


class TestScanLibrary:
    def test_scan_adds_new_books(self, db_session, tmp_path):
        """Scanning a directory with M4B files adds them to the DB."""
        # Create fake M4B files
        (tmp_path / "book1.m4b").write_bytes(b"fake1")
        (tmp_path / "book2.m4b").write_bytes(b"fake2")

        with (
            patch("audiplex.scanner.extract_metadata") as mock_meta,
            patch("audiplex.scanner.compute_file_hash", return_value="hash1"),
            patch("audiplex.scanner.extract_cover_art"),
        ):
            mock_meta.side_effect = [
                _make_fake_metadata(title="Book One"),
                _make_fake_metadata(title="Book Two"),
            ]

            result = scan_library(db_session, [str(tmp_path)], str(tmp_path / "covers"))

        assert result.added == 2
        assert result.updated == 0
        assert result.removed == 0
        assert db_session.query(Book).count() == 2

    def test_scan_skips_unchanged(self, db_session, tmp_path):
        """Files with same hash are skipped on rescan."""
        (tmp_path / "book.m4b").write_bytes(b"fake")

        with (
            patch("audiplex.scanner.extract_metadata") as mock_meta,
            patch("audiplex.scanner.compute_file_hash", return_value="samehash"),
            patch("audiplex.scanner.extract_cover_art"),
        ):
            mock_meta.return_value = _make_fake_metadata()
            scan_library(db_session, [str(tmp_path)], str(tmp_path / "covers"))

        # Second scan — same hash
        with (
            patch("audiplex.scanner.extract_metadata") as mock_meta,
            patch("audiplex.scanner.compute_file_hash", return_value="samehash"),
            patch("audiplex.scanner.extract_cover_art"),
        ):
            result = scan_library(db_session, [str(tmp_path)], str(tmp_path / "covers"))

        assert result.added == 0
        assert result.updated == 0
        # extract_metadata should NOT have been called on the second scan
        mock_meta.assert_not_called()

    def test_scan_updates_changed(self, db_session, tmp_path):
        """Files with new hash are re-processed."""
        (tmp_path / "book.m4b").write_bytes(b"fake")

        with (
            patch("audiplex.scanner.extract_metadata") as mock_meta,
            patch("audiplex.scanner.compute_file_hash", return_value="hash_v1"),
            patch("audiplex.scanner.extract_cover_art"),
        ):
            mock_meta.return_value = _make_fake_metadata(title="Old Title")
            scan_library(db_session, [str(tmp_path)], str(tmp_path / "covers"))

        # Second scan — different hash
        with (
            patch("audiplex.scanner.extract_metadata") as mock_meta,
            patch("audiplex.scanner.compute_file_hash", return_value="hash_v2"),
            patch("audiplex.scanner.extract_cover_art"),
        ):
            mock_meta.return_value = _make_fake_metadata(title="New Title")
            result = scan_library(db_session, [str(tmp_path)], str(tmp_path / "covers"))

        assert result.updated == 1
        book = db_session.query(Book).first()
        assert book.title == "New Title"

    def test_scan_removes_missing(self, db_session, tmp_path):
        """Books whose files were deleted get removed from DB."""
        m4b = tmp_path / "gone.m4b"
        m4b.write_bytes(b"fake")

        with (
            patch("audiplex.scanner.extract_metadata") as mock_meta,
            patch("audiplex.scanner.compute_file_hash", return_value="hash1"),
            patch("audiplex.scanner.extract_cover_art"),
        ):
            mock_meta.return_value = _make_fake_metadata()
            scan_library(db_session, [str(tmp_path)], str(tmp_path / "covers"))

        assert db_session.query(Book).count() == 1

        # Delete the file and rescan
        m4b.unlink()

        with (
            patch("audiplex.scanner.extract_metadata"),
            patch("audiplex.scanner.compute_file_hash"),
            patch("audiplex.scanner.extract_cover_art"),
        ):
            result = scan_library(db_session, [str(tmp_path)], str(tmp_path / "covers"))

        assert result.removed == 1
        assert db_session.query(Book).count() == 0

    def test_scan_nonexistent_path(self, db_session, tmp_path):
        """Nonexistent library path produces an error, not a crash.

        #842 changed the wording: the root is now rejected by the readability
        gate before dispatch, so it reports "unreadable, skipping" rather than
        the scanner's own "does not exist". Same contract — one error, no crash.
        """
        result = scan_library(db_session, [str(tmp_path / "nope")], str(tmp_path / "covers"))
        assert len(result.errors) == 1
        assert "unreadable" in result.errors[0].lower()
        assert "nope" in result.errors[0]

    def test_music_only_scan_preserves_audiobooks(self, db_session, sample_book, music_root_layout):
        """A music-only (or misconfigured) scan must not sweep Book rows —
        mirrors the existing has_music_root guard on the album sweep.
        Regression test for the audiobook-deletion bug Codex flagged (#535):
        the Book sweep had no has_audiobook_root guard, so scanning only a
        music root would silently delete every audiobook."""
        assert db_session.query(Book).count() == 1

        with patch("audiplex.scanners.music.mutagen.File") as mock_file:
            audio = MagicMock()
            audio.info.length = 300.0
            audio.get.side_effect = lambda key, default=None: default
            audio.__contains__ = lambda self, key: False
            mock_file.return_value = audio

            result = scan_library(
                db_session,
                [LibraryRoot(path=str(music_root_layout), category="music")],
                str(music_root_layout / "covers"),
            )

        assert result.errors == []
        assert result.removed == 0
        assert db_session.query(Book).count() == 1

    def test_scan_normalizes_coauthor_strings(self, db_session, tmp_path):
        """Co-author rolls collapse to the primary author so they group together."""
        (tmp_path / "solo.m4b").write_bytes(b"solo")
        (tmp_path / "coauth.m4b").write_bytes(b"coauth")

        with (
            patch("audiplex.scanner.extract_metadata") as mock_meta,
            patch("audiplex.scanner.compute_file_hash", return_value="hash1"),
            patch("audiplex.scanner.extract_cover_art"),
        ):
            # The real extract_metadata applies normalize_author internally;
            # here we simulate that contract — pre-normalized values reach the
            # scanner.
            mock_meta.side_effect = [
                _make_fake_metadata(title="Solo Book", author="Brandon Sanderson"),
                _make_fake_metadata(title="Co-Author Book", author="Brandon Sanderson"),
            ]
            scan_library(db_session, [str(tmp_path)], str(tmp_path / "covers"))

        authors = {b.author for b in db_session.query(Book).all()}
        assert authors == {"Brandon Sanderson"}

    def test_scan_with_cue_file(self, db_session, tmp_path):
        """CUE files next to M4B files are parsed for chapters."""
        (tmp_path / "book.m4b").write_bytes(b"fake")
        (tmp_path / "book.cue").write_text(
            '''FILE "book.MP3" MP3
  TRACK 01 AUDIO
    TITLE "Part 1"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Part 2"
    INDEX 01 10:00:00
''',
            encoding="utf-8",
        )

        with (
            patch("audiplex.scanner.extract_metadata") as mock_meta,
            patch("audiplex.scanner.compute_file_hash", return_value="hash1"),
            patch("audiplex.scanner.extract_cover_art"),
        ):
            mock_meta.return_value = _make_fake_metadata(duration_seconds=1200.0)
            result = scan_library(db_session, [str(tmp_path)], str(tmp_path / "covers"))

        assert result.added == 1
        chapters = db_session.query(Chapter).all()
        assert len(chapters) == 2
        assert chapters[0].title == "Part 1"
        assert chapters[1].title == "Part 2"
        assert chapters[0].end_seconds == 600.0  # starts of next chapter
        assert chapters[1].end_seconds == 1200.0  # duration

    def test_rescan_reparses_series_from_raw(self, db_session, tmp_path):
        """The re-derivation pass should fix existing rows even when the file
        hash is unchanged — important after a parser improvement."""
        (tmp_path / "book.m4b").write_bytes(b"fake")

        # Seed a book whose series field was left as the raw album tag
        # (the pre-parser shape that the migration produces).
        book = Book(
            title="Caliban's War",
            author="James S.A. Corey",
            series="Caliban's War: The Expanse, Book 2 (Unabridged)",
            series_sequence=None,
            series_raw="Caliban's War: The Expanse, Book 2 (Unabridged)",
            duration_seconds=3600.0,
            file_path=str(tmp_path / "book.m4b"),
            file_size=4,
            file_hash="samehash",
        )
        db_session.add(book)
        db_session.commit()

        with (
            patch("audiplex.scanner.extract_metadata") as mock_meta,
            patch("audiplex.scanner.compute_file_hash", return_value="samehash"),
            patch("audiplex.scanner.extract_cover_art"),
        ):
            mock_meta.return_value = _make_fake_metadata()
            scan_library(db_session, [str(tmp_path)], str(tmp_path / "covers"))

        db_session.refresh(book)
        assert book.series == "The Expanse"
        assert book.series_sequence == "2"


class TestUnreadableRootGuard:
    """#842 — an unreadable root must never trigger a sweep of its entries.

    The old guard was "a root of this category was iterated", which is still
    true when the drive is unplugged: the scanner bails out, the found-set
    comes back empty, and every row under that root looks deleted. That is the
    598-audiobook hazard.
    """

    def _scan(self, db_session, roots, cover_dir):
        with (
            patch("audiplex.scanner.extract_metadata") as mock_meta,
            patch("audiplex.scanner.compute_file_hash", return_value="hash1"),
            patch("audiplex.scanner.extract_cover_art"),
        ):
            mock_meta.return_value = _make_fake_metadata()
            return scan_library(db_session, roots, cover_dir)

    def test_helpers_reject_missing_root(self, tmp_path):
        from audiplex.scanner import _root_is_readable, _under_any

        assert _root_is_readable(str(tmp_path)) is True
        assert _root_is_readable(str(tmp_path / "nope")) is False
        # A file is not an enumerable root.
        f = tmp_path / "f.txt"
        f.write_text("x")
        assert _root_is_readable(str(f)) is False

        root = str(tmp_path)
        assert _under_any(str(tmp_path / "a" / "b.m4b"), [root]) is True
        assert _under_any(root, [root]) is True
        assert _under_any("/somewhere/else.m4b", [root]) is False
        # A sibling that merely shares a name prefix is NOT under the root.
        assert _under_any(str(tmp_path) + "_other/x.m4b", [root]) is False

    def test_vanished_root_preserves_its_books(self, db_session, tmp_path):
        """The core hazard: root disappears, entries must survive."""
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "book.m4b").write_bytes(b"fake")
        covers = str(tmp_path / "covers")

        self._scan(db_session, [str(lib)], covers)
        assert db_session.query(Book).count() == 1

        # Whole root goes away (drive unplugged / share dropped).
        (lib / "book.m4b").unlink()
        lib.rmdir()

        result = self._scan(db_session, [str(lib)], covers)

        assert result.removed == 0, "unreadable root must not sweep"
        assert db_session.query(Book).count() == 1, "book must survive"
        assert any("unreadable" in e.lower() for e in result.errors)

    def test_readable_root_still_sweeps_while_unreadable_one_is_spared(
        self, db_session, tmp_path
    ):
        """The guard must not become a blanket amnesty: a live root still
        garbage-collects its own deletions."""
        alive = tmp_path / "alive"
        dead = tmp_path / "dead"
        alive.mkdir()
        dead.mkdir()
        (alive / "a.m4b").write_bytes(b"fake")
        (dead / "d.m4b").write_bytes(b"fake")
        covers = str(tmp_path / "covers")

        self._scan(db_session, [str(alive), str(dead)], covers)
        assert db_session.query(Book).count() == 2

        # alive loses its file (a real deletion); dead loses its whole root.
        (alive / "a.m4b").unlink()
        (dead / "d.m4b").unlink()
        dead.rmdir()

        result = self._scan(db_session, [str(alive), str(dead)], covers)

        assert result.removed == 1, "the live root's own deletion is still swept"
        surviving = db_session.query(Book).all()
        assert len(surviving) == 1
        assert "dead" in surviving[0].file_path, "entry under the dead root survived"


class TestUnreadableMusicRootGuard:
    def test_vanished_music_root_preserves_albums(self, db_session, tmp_path):
        """Same guard, album side — this is the shape that threatened q:/music
        while E:/Stacked Deck/Music was configured but absent."""
        from audiplex.models import Album

        music = tmp_path / "music"
        music.mkdir()
        (music / "song.m4a").write_bytes(b"fake")
        covers = str(tmp_path / "covers")
        roots = [LibraryRoot(path=str(music), category="music")]

        audio = MagicMock()
        audio.info.length = 300.0
        audio.get.side_effect = lambda key, default=None: default
        audio.__contains__ = lambda self, key: False

        with patch("audiplex.scanners.music.mutagen.File", return_value=audio):
            scan_library(db_session, roots, covers)
            assert db_session.query(Album).count() == 1

            (music / "song.m4a").unlink()
            music.rmdir()
            result = scan_library(db_session, roots, covers)

        assert result.removed == 0
        assert db_session.query(Album).count() == 1, "album survived a vanished root"
