"""Tests for the flat meditation scanner (one Book per audio file)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from audiplex.config import LibraryRoot
from audiplex.models import Book, Chapter
from audiplex.scanner import scan_library
from audiplex.scanners.meditation import _title_from_stem, scan_meditation


def _fake_audio(duration: float = 600.0, **tags):
    """Build a fake mutagen File(easy=True) result (tag lookups return lists)."""
    audio = MagicMock()
    audio.info.length = duration
    audio.get.side_effect = lambda key, default=None: (
        [tags[key]] if key in tags else default
    )
    return audio


def _make_files(folder: Path, names: list[str]):
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"")


class TestTitleFromStem:
    def test_underscore_separator_becomes_dash(self):
        assert (
            _title_from_stem("Daily Calm _ 10 Minute Mindfulness Meditation")
            == "Daily Calm - 10 Minute Mindfulness Meditation"
        )

    def test_quote_style_underscores_are_dropped(self):
        assert (
            _title_from_stem("The _Feel Good_ Reset _ A 10 Minute Meditation")
            == "The Feel Good Reset - A 10 Minute Meditation"
        )

    def test_strips_leading_track_numbering(self):
        assert _title_from_stem("03 - A Positive Day Ahead") == "A Positive Day Ahead"

    def test_keeps_leading_number_that_is_content(self):
        """"10 Minute ..." opens nearly every real title — it is not a track number."""
        assert (
            _title_from_stem("10 Minute Guided Meditation for Relaxation")
            == "10 Minute Guided Meditation for Relaxation"
        )

    def test_plain_stem_passes_through(self):
        assert _title_from_stem("Ocean Waves") == "Ocean Waves"


class TestScanMeditation:
    def test_each_file_becomes_its_own_book(self, db_session, tmp_path):
        """Ten flat files must produce ten Books — not one book with ten chapters."""
        names = [f"Meditation {i}.m4a" for i in range(1, 11)]
        _make_files(tmp_path, names)

        with patch("audiplex.scanners.meditation.mutagen.File") as mock_file:
            mock_file.return_value = _fake_audio(duration=600.0)
            result, found_paths = scan_meditation(
                db_session, str(tmp_path), str(tmp_path / "covers")
            )

        assert result.added == 10
        assert result.errors == []
        assert len(found_paths) == 10

        books = db_session.query(Book).filter(Book.category == "meditation").all()
        assert len(books) == 10
        # Exactly one chapter each, spanning the whole file, with no per-track
        # file_path — that is what keeps them on the whole-file stream endpoint.
        for book in books:
            chapters = db_session.query(Chapter).filter(Chapter.book_id == book.id).all()
            assert len(chapters) == 1
            assert chapters[0].start_seconds == 0.0
            assert chapters[0].end_seconds == 600.0
            assert chapters[0].file_path is None
            assert book.duration_seconds == 600.0

    def test_filename_beats_junk_tag_title(self, db_session, tmp_path):
        """The real corpus carries inherited junk tags — the filename must win."""
        _make_files(tmp_path, ["Daily Calm _ 10 Minute Mindfulness Meditation.m4a"])

        with patch("audiplex.scanners.meditation.mutagen.File") as mock_file:
            mock_file.return_value = _fake_audio(
                duration=300.0, title="|", artist="N", album="GAUNERZINKEN"
            )
            scan_meditation(db_session, str(tmp_path), str(tmp_path / "covers"))

        book = db_session.query(Book).one()
        assert book.title == "Daily Calm - 10 Minute Mindfulness Meditation"
        assert book.author is None  # single-letter artist is junk, not a credit

    def test_substantive_author_tag_is_kept(self, db_session, tmp_path):
        _make_files(tmp_path, ["Loving Kindness.m4a"])

        with patch("audiplex.scanners.meditation.mutagen.File") as mock_file:
            mock_file.return_value = _fake_audio(duration=300.0, artist="Dr KJ Foster")
            scan_meditation(db_session, str(tmp_path), str(tmp_path / "covers"))

        assert db_session.query(Book).one().author == "Dr KJ Foster"

    def test_falls_back_to_filename_when_untagged(self, db_session, tmp_path):
        _make_files(tmp_path, ["A Positive Day Ahead _ 10 Minute Meditation.m4a"])

        with patch("audiplex.scanners.meditation.mutagen.File") as mock_file:
            mock_file.return_value = _fake_audio(duration=300.0)
            scan_meditation(db_session, str(tmp_path), str(tmp_path / "covers"))

        book = db_session.query(Book).one()
        assert book.title == "A Positive Day Ahead - 10 Minute Meditation"

    def test_rescan_is_idempotent(self, db_session, tmp_path):
        _make_files(tmp_path, ["one.m4a", "two.m4a"])

        with patch("audiplex.scanners.meditation.mutagen.File") as mock_file:
            mock_file.return_value = _fake_audio(duration=600.0)
            first, _ = scan_meditation(db_session, str(tmp_path), str(tmp_path / "covers"))
            db_session.commit()
            second, _ = scan_meditation(db_session, str(tmp_path), str(tmp_path / "covers"))

        assert first.added == 2
        assert second.added == 0
        assert second.updated == 0
        assert db_session.query(Book).count() == 2
        assert db_session.query(Chapter).count() == 2

    def test_ignores_non_audio_and_subfolders(self, db_session, tmp_path):
        _make_files(tmp_path, ["keeper.m4a", "notes.txt", "cover.jpg"])
        _make_files(tmp_path / "nested", ["buried.m4a"])

        with patch("audiplex.scanners.meditation.mutagen.File") as mock_file:
            mock_file.return_value = _fake_audio(duration=600.0)
            result, found_paths = scan_meditation(
                db_session, str(tmp_path), str(tmp_path / "covers")
            )

        assert result.added == 1
        assert len(found_paths) == 1
        assert db_session.query(Book).one().title == "keeper"

    def test_missing_root_reports_error(self, db_session, tmp_path):
        result, found_paths = scan_meditation(
            db_session, str(tmp_path / "nope"), str(tmp_path / "covers")
        )
        assert result.added == 0
        assert len(result.errors) == 1
        assert found_paths == set()

    def test_unreadable_file_does_not_abort_the_scan(self, db_session, tmp_path):
        _make_files(tmp_path, ["bad.m4a", "good.m4a"])

        def _file_side_effect(path, **kwargs):
            if "bad" in str(path):
                raise ValueError("corrupt")
            return _fake_audio(duration=600.0)

        with patch("audiplex.scanners.meditation.mutagen.File", side_effect=_file_side_effect):
            result, _ = scan_meditation(db_session, str(tmp_path), str(tmp_path / "covers"))

        assert result.added == 1
        assert len(result.errors) == 1


class TestScanLibraryDispatch:
    def test_meditation_root_routes_to_meditation_scanner(self, db_session, tmp_path):
        """A meditation root must dispatch, and its books must survive the sweep."""
        _make_files(tmp_path, ["calm.m4a"])

        with patch("audiplex.scanners.meditation.mutagen.File") as mock_file:
            mock_file.return_value = _fake_audio(duration=600.0)
            result = scan_library(
                db_session,
                [LibraryRoot(path=str(tmp_path), category="meditation")],
                str(tmp_path / "covers"),
            )

        assert result.added == 1
        assert result.removed == 0
        book = db_session.query(Book).one()
        assert book.category == "meditation"
