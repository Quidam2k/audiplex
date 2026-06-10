"""Tests for the audiobook_misc multi-track folder scanner."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from audiplex.config import LibraryRoot
from audiplex.models import Book, Chapter
from audiplex.scanner import scan_library
from audiplex.scanners.audiobook_misc import _natural_key, scan_misc


def _fake_audio(duration: float = 600.0, **tags):
    """Build a fake mutagen File(easy=True) result.

    `tags` lookup returns a list (mutagen convention).
    """
    audio = MagicMock()
    audio.info.length = duration
    audio.get.side_effect = lambda key, default=None: (
        [tags[key]] if key in tags else default
    )
    audio.__contains__ = lambda self, key: key in tags
    return audio


def _make_track_files(folder: Path, names: list[str]):
    """Touch fake audio files in `folder` (just empty bytes — mutagen is mocked)."""
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"")


class TestNaturalKey:
    def test_zero_padded_vs_unpadded(self):
        names = ["track2.mp3", "track10.mp3", "track1.mp3"]
        names.sort(key=_natural_key)
        assert names == ["track1.mp3", "track2.mp3", "track10.mp3"]

    def test_pure_numeric(self):
        names = ["10.mp3", "2.mp3", "1.mp3"]
        names.sort(key=_natural_key)
        assert names == ["1.mp3", "2.mp3", "10.mp3"]


class TestScanMisc:
    def test_picks_up_book(self, db_session, tmp_path):
        """Three audio files in a folder become one Book + three Chapters with cumulative starts."""
        book_folder = tmp_path / "My Book"
        _make_track_files(book_folder, ["01.mp3", "02.mp3", "03.mp3"])

        with patch("audiplex.scanners.audiobook_misc.mutagen.File") as mock_file:
            mock_file.return_value = _fake_audio(duration=600.0, title="Track", album="My Book", artist="Some Author")
            result, found_paths = scan_misc(db_session, str(tmp_path), str(tmp_path / "covers"))
            db_session.commit()

        assert result.added == 1
        book = db_session.query(Book).first()
        assert book is not None
        assert book.category == "audiobook_misc"
        assert book.title == "My Book"
        assert book.duration_seconds == pytest.approx(1800.0)
        chapters = db_session.query(Chapter).order_by(Chapter.index).all()
        assert len(chapters) == 3
        assert chapters[0].start_seconds == pytest.approx(0.0)
        assert chapters[1].start_seconds == pytest.approx(600.0)
        assert chapters[2].start_seconds == pytest.approx(1200.0)
        assert str(book_folder) in found_paths

    def test_natural_sort(self, db_session, tmp_path):
        """1.mp3, 2.mp3, 10.mp3 sort 1/2/10 (not lexicographic)."""
        book_folder = tmp_path / "Book"
        _make_track_files(book_folder, ["1.mp3", "2.mp3", "10.mp3"])

        durations = {"1.mp3": 100.0, "2.mp3": 200.0, "10.mp3": 300.0}

        def fake_file(path, easy=True):
            name = Path(path).name
            return _fake_audio(duration=durations[name], title=name)

        with patch("audiplex.scanners.audiobook_misc.mutagen.File", side_effect=fake_file):
            scan_misc(db_session, str(tmp_path), str(tmp_path / "covers"))
            db_session.commit()

        chapters = db_session.query(Chapter).order_by(Chapter.index).all()
        assert [c.title for c in chapters] == ["1.mp3", "2.mp3", "10.mp3"]
        # Cumulative starts reflect the natural order
        assert chapters[0].start_seconds == pytest.approx(0.0)
        assert chapters[1].start_seconds == pytest.approx(100.0)
        assert chapters[2].start_seconds == pytest.approx(300.0)

    def test_falls_back_to_folder_name(self, db_session, tmp_path):
        """Missing tags → title from folder, author from parent folder."""
        author_folder = tmp_path / "Some Author"
        book_folder = author_folder / "Untitled Work"
        _make_track_files(book_folder, ["a.mp3", "b.mp3"])

        with patch("audiplex.scanners.audiobook_misc.mutagen.File") as mock_file:
            # No tags at all
            mock_file.return_value = _fake_audio(duration=300.0)
            scan_misc(db_session, str(tmp_path), str(tmp_path / "covers"))
            db_session.commit()

        book = db_session.query(Book).first()
        assert book.title == "Untitled Work"
        assert book.author == "Some Author"

    def test_walks_nested(self, db_session, tmp_path):
        """Author/Book/track.mp3 → one book; author from Author folder."""
        nested = tmp_path / "AuthorX" / "BookY"
        _make_track_files(nested, ["track.mp3"])

        with patch("audiplex.scanners.audiobook_misc.mutagen.File") as mock_file:
            mock_file.return_value = _fake_audio(duration=120.0)
            scan_misc(db_session, str(tmp_path), str(tmp_path / "covers"))
            db_session.commit()

        books = db_session.query(Book).all()
        assert len(books) == 1
        assert books[0].title == "BookY"
        assert books[0].author == "AuthorX"

    def test_skips_folders_without_audio(self, db_session, tmp_path):
        """Folder with no audio files (or only PDFs) is silently skipped."""
        empty_folder = tmp_path / "Empty"
        empty_folder.mkdir()
        pdf_folder = tmp_path / "PdfOnly"
        pdf_folder.mkdir()
        (pdf_folder / "doc.pdf").write_bytes(b"")

        with patch("audiplex.scanners.audiobook_misc.mutagen.File"):
            result, _ = scan_misc(db_session, str(tmp_path), str(tmp_path / "covers"))

        assert result.added == 0
        assert db_session.query(Book).count() == 0

    def test_chapter_file_paths_set(self, db_session, tmp_path):
        """Every chapter for a misc book has file_path populated."""
        book_folder = tmp_path / "Book"
        _make_track_files(book_folder, ["a.mp3", "b.mp3"])

        with patch("audiplex.scanners.audiobook_misc.mutagen.File") as mock_file:
            mock_file.return_value = _fake_audio(duration=100.0)
            scan_misc(db_session, str(tmp_path), str(tmp_path / "covers"))
            db_session.commit()

        chapters = db_session.query(Chapter).all()
        assert len(chapters) == 2
        for c in chapters:
            assert c.file_path is not None
            assert Path(c.file_path).suffix == ".mp3"


class TestOrchestrator:
    def test_dispatch_to_misc_scanner(self, db_session, tmp_path):
        """scan_library routes audiobook_misc roots to scan_misc."""
        book_folder = tmp_path / "Book"
        _make_track_files(book_folder, ["1.mp3"])

        with patch("audiplex.scanners.audiobook_misc.mutagen.File") as mock_file:
            mock_file.return_value = _fake_audio(duration=60.0)
            roots = [LibraryRoot(path=str(tmp_path), category="audiobook_misc")]
            result = scan_library(db_session, roots, str(tmp_path / "covers"))

        assert result.added == 1
        book = db_session.query(Book).first()
        assert book.category == "audiobook_misc"

    def test_unknown_category_records_error(self, db_session, tmp_path):
        roots = [LibraryRoot(path=str(tmp_path), category="bogus")]
        result = scan_library(db_session, roots, str(tmp_path / "covers"))
        assert any("Unknown category" in e for e in result.errors)

    def test_legacy_string_roots_treated_as_libation(self, db_session, tmp_path):
        """Legacy callers passing list[str] still work."""
        # Empty dir — just verify the call doesn't crash and no books created.
        result = scan_library(db_session, [str(tmp_path)], str(tmp_path / "covers"))
        assert result.added == 0
        assert result.errors == []

    def test_two_roots_no_cross_deletion(self, db_session, tmp_path):
        """Removed-book sweep unions found_paths across roots."""
        libation_root = tmp_path / "libation"
        misc_root = tmp_path / "misc"
        libation_root.mkdir()
        (libation_root / "book.m4b").write_bytes(b"x")
        book_folder = misc_root / "MiscBook"
        _make_track_files(book_folder, ["1.mp3"])

        from audiplex.utils.metadata import BookMetadata

        with (
            patch("audiplex.scanner.extract_metadata") as mock_meta,
            patch("audiplex.scanner.compute_file_hash", return_value="hash1"),
            patch("audiplex.scanner.extract_cover_art"),
            patch("audiplex.scanners.audiobook_misc.mutagen.File") as mock_misc_file,
        ):
            mock_meta.return_value = BookMetadata(
                title="Lib Book", author="A", duration_seconds=100.0,
                file_size=10, file_hash="h", has_cover=False, embedded_chapters=[],
            )
            mock_misc_file.return_value = _fake_audio(duration=60.0)

            roots = [
                LibraryRoot(path=str(libation_root), category="audiobook_clean"),
                LibraryRoot(path=str(misc_root), category="audiobook_misc"),
            ]
            result = scan_library(db_session, roots, str(tmp_path / "covers"))

        assert result.added == 2
        assert result.removed == 0
        assert db_session.query(Book).count() == 2
