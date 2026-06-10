"""Tests for the music scanner."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from audiplex.config import LibraryRoot
from audiplex.models import Album, Artist, Track
from audiplex.scanner import scan_library
from audiplex.scanners.music import (
    _is_disc_folder,
    _natural_key,
    scan_music,
)


def _fake_audio(duration: float = 300.0, **tags):
    """Build a fake mutagen File(easy=True) result."""
    audio = MagicMock()
    audio.info.length = duration
    audio.get.side_effect = lambda key, default=None: (
        [tags[key]] if key in tags else default
    )
    audio.__contains__ = lambda self, key: key in tags
    return audio


@pytest.fixture
def mocked_mutagen():
    """Patch mutagen.File in the music scanner with a default fake audio
    object (300s, no tags). Tests can override `mock_file.side_effect`.
    """
    with patch("audiplex.scanners.music.mutagen.File") as mock_file:
        mock_file.return_value = _fake_audio(duration=300.0)
        yield mock_file


class TestHelpers:
    def test_disc_folder_regex_cases(self):
        assert _is_disc_folder("CD1") == 1
        assert _is_disc_folder("CD 2") == 2
        assert _is_disc_folder("cd 3") == 3
        assert _is_disc_folder("-cd 1") == 1
        assert _is_disc_folder("Disc 2") == 2
        assert _is_disc_folder("disc_4") == 4
        assert _is_disc_folder("Vol 1") is None
        assert _is_disc_folder("Kind of Blue") is None

    def test_natural_key_basic(self):
        names = ["10.mp3", "2.mp3", "1.mp3"]
        names.sort(key=_natural_key)
        assert names == ["1.mp3", "2.mp3", "10.mp3"]


class TestScanMusic:
    def test_creates_artist_album_track_chain(self, db_session, music_root_layout, mocked_mutagen):
        scan_music(db_session, str(music_root_layout), str(music_root_layout / "covers"))
        db_session.commit()

        miles = db_session.query(Artist).filter(Artist.name == "Miles Davis").first()
        assert miles is not None

        kob = db_session.query(Album).filter(Album.title == "Kind of Blue").first()
        assert kob is not None
        assert kob.artist_id == miles.id
        assert kob.track_count == 2
        assert kob.genre == "Jazz"

        tracks = db_session.query(Track).filter(Track.album_id == kob.id).all()
        assert len(tracks) == 2
        assert all(t.artist_id == miles.id for t in tracks)

    def test_genre_only_under_artists_and_albums(self, db_session, music_root_layout, mocked_mutagen):
        scan_music(db_session, str(music_root_layout), str(music_root_layout / "covers"))
        db_session.commit()

        kob = db_session.query(Album).filter(Album.title == "Kind of Blue").first()
        assert kob.genre == "Jazz"

        bike = db_session.query(Album).filter(Album.title == "Bike Music").first()
        assert bike is not None
        assert bike.genre is None

    def test_loose_top_level_becomes_various_artists(self, db_session, music_root_layout, mocked_mutagen):
        scan_music(db_session, str(music_root_layout), str(music_root_layout / "covers"))
        db_session.commit()

        bike = db_session.query(Album).filter(Album.title == "Bike Music").first()
        assert bike is not None
        va = db_session.query(Artist).filter(Artist.id == bike.artist_id).first()
        assert va.name == "Various Artists"

    def test_multi_disc_merge_cd1_cd2(self, db_session, music_root_layout, mocked_mutagen):
        scan_music(db_session, str(music_root_layout), str(music_root_layout / "covers"))
        db_session.commit()

        sym = db_session.query(Album).filter(Album.title == "Symphonies").first()
        assert sym is not None
        assert sym.track_count == 2
        tracks = db_session.query(Track).filter(Track.album_id == sym.id).order_by(Track.disc_number).all()
        discs = [t.disc_number for t in tracks]
        assert discs == [1, 2]

    def test_multi_disc_pattern_dash_cd_n(self, db_session, music_root_layout, mocked_mutagen):
        scan_music(db_session, str(music_root_layout), str(music_root_layout / "covers"))
        db_session.commit()

        concertos = db_session.query(Album).filter(Album.title == "Concertos").first()
        assert concertos is not None
        assert concertos.track_count == 2
        tracks = db_session.query(Track).filter(Track.album_id == concertos.id).order_by(Track.disc_number).all()
        assert [t.disc_number for t in tracks] == [1, 2]

    def test_wma_skipped(self, db_session, music_root_layout, mocked_mutagen):
        scan_music(db_session, str(music_root_layout), str(music_root_layout / "covers"))
        db_session.commit()

        mixed = db_session.query(Album).filter(Album.title == "Mixed").first()
        assert mixed is not None
        assert mixed.track_count == 1
        tracks = db_session.query(Track).filter(Track.album_id == mixed.id).all()
        assert len(tracks) == 1
        assert tracks[0].file_path.endswith("real.mp3")

    def test_cover_priority_sidecar_then_embedded(
        self, db_session, music_root_layout, mocked_mutagen
    ):
        cover_dir = music_root_layout / "covers"

        # Embedded cover available for the "Embedded Only" album's track
        # (track has no sidecar). Patch extract_embedded_cover where it's
        # consumed in scanners.music.
        def fake_extract(track_path):
            if "Embedded Only" in str(track_path):
                return (b"\xff\xd8\xff" + b"EMBEDDED", "jpg")
            return None

        with patch(
            "audiplex.scanners.music.extract_embedded_cover",
            side_effect=fake_extract,
        ):
            scan_music(db_session, str(music_root_layout), str(cover_dir))
            db_session.commit()

        kob = db_session.query(Album).filter(Album.title == "Kind of Blue").first()
        assert kob.has_cover is True
        kob_cover = cover_dir / f"album_{kob.id}.jpg"
        assert kob_cover.exists()
        # Sidecar bytes were "FAKEJPG"; embedded would be "EMBEDDED".
        assert b"FAKEJPG" in kob_cover.read_bytes()

        emb = db_session.query(Album).filter(Album.title == "Embedded Only").first()
        assert emb.has_cover is True
        emb_cover = cover_dir / f"album_{emb.id}.jpg"
        assert emb_cover.exists()
        assert b"EMBEDDED" in emb_cover.read_bytes()

    def test_rescan_unchanged_is_noop(self, db_session, music_root_layout, mocked_mutagen):
        result1, _ = scan_music(
            db_session, str(music_root_layout), str(music_root_layout / "covers")
        )
        db_session.commit()
        first_added = result1.added
        assert first_added > 0

        result2, _ = scan_music(
            db_session, str(music_root_layout), str(music_root_layout / "covers")
        )
        db_session.commit()
        assert result2.added == 0
        assert result2.updated == 0

    def test_rescan_after_track_added_updates_album(
        self, db_session, music_root_layout, mocked_mutagen
    ):
        cover_dir = str(music_root_layout / "covers")
        scan_music(db_session, str(music_root_layout), cover_dir)
        db_session.commit()

        kob = db_session.query(Album).filter(Album.title == "Kind of Blue").first()
        original_count = kob.track_count

        # Add a new track to the album folder.
        new_track = Path(kob.folder_path) / "03 Blue in Green.mp3"
        new_track.write_bytes(b"")

        result, _ = scan_music(db_session, str(music_root_layout), cover_dir)
        db_session.commit()
        assert result.updated >= 1

        kob2 = db_session.query(Album).filter(Album.title == "Kind of Blue").first()
        assert kob2.track_count == original_count + 1


class TestOrchestrator:
    def test_orphan_album_removed_and_artist_gc(
        self, db_session, music_root_layout, mocked_mutagen
    ):
        cover_dir = str(music_root_layout / "covers")
        roots = [LibraryRoot(path=str(music_root_layout), category="music")]

        scan_library(db_session, roots, cover_dir)
        miles = db_session.query(Artist).filter(Artist.name == "Miles Davis").first()
        assert miles is not None
        kob_path = (
            music_root_layout
            / "Artists & Albums" / "Jazz" / "Miles Davis" / "Kind of Blue"
        )

        # Delete the Kind of Blue folder (and Miles Davis parent — Miles
        # has only one album under him, so artist should also disappear).
        for f in kob_path.iterdir():
            f.unlink()
        kob_path.rmdir()
        # parent (Miles Davis) is now empty
        kob_path.parent.rmdir()

        scan_library(db_session, roots, cover_dir)

        assert (
            db_session.query(Album).filter(Album.title == "Kind of Blue").first()
            is None
        )
        assert (
            db_session.query(Artist).filter(Artist.name == "Miles Davis").first()
            is None
        )

    def test_dispatch_to_music_scanner(self, db_session, music_root_layout, mocked_mutagen):
        roots = [LibraryRoot(path=str(music_root_layout), category="music")]
        result = scan_library(db_session, roots, str(music_root_layout / "covers"))
        assert result.added > 0
        assert db_session.query(Album).count() > 0
