"""Repair overlay + taste-data survival across rescans (#3037).

The hazard these pin down was measured, not imagined: the music scanner used to
delete every track in a changed album and re-insert it. PlayStat rows went with
the ORM cascade, TrackRating rows were left pointing at dead ids, and because
`tracks.id` is a plain INTEGER PRIMARY KEY, SQLite hands those ids out again —
so a stray rating could reattach to a different song. Dropping one file into the
music folder was enough to trigger it.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from audiplex.models import Album, Artist, PlayStat, Track, TrackRating, TrackTagRepair
from audiplex.scanners.music import scan_music
from audiplex.tag_repair import APPLIED, HIGH, LOW, PENDING_REVIEW

YT = "https://www.youtube.com/watch?v=HUHC9tYz8ik"


def _fake_audio(duration: float = 300.0, **tags):
    audio = MagicMock()
    audio.info.length = duration
    audio.get.side_effect = lambda key, default=None: (
        [tags[key]] if key in tags else default
    )
    audio.__contains__ = lambda self, key: key in tags
    return audio


@pytest.fixture
def rip_root(tmp_path):
    """A flat dump of YouTube rips loose in the music root — Todd's actual
    library shape, and the one that produced 207 nameless tracks."""
    root = tmp_path / "music"
    root.mkdir()
    for name in (
        "Billie Eilish - bury a friend (Official Music Video) (128kbit_AAC).m4a",
        "Ashnikko - Special (Official Video) (128kbit_AAC).m4a",
        "Barracuda (128kbit_AAC).m4a",
    ):
        (root / name).write_bytes(b"")
    return root


TAGS_BY_STEM = {
    "Billie Eilish - bury a friend (Official Music Video) (128kbit_AAC)": {
        "title": "Billie Eilish - bury a friend (Official Music Video)",
        "artist": "BillieEilishVEVO",
        "comment": YT,
    },
    "Ashnikko - Special (Official Video) (128kbit_AAC)": {
        "title": "Ashnikko - Special (Official Video)",
        "artist": "Ashnikko",
        "comment": YT,
    },
    # No dash in the title and only an account name to go on — the review bucket.
    "Barracuda (128kbit_AAC)": {
        "title": "Barracuda",
        "artist": "Heart",
        "comment": YT,
    },
}


@pytest.fixture
def mocked_rip_tags():
    def by_path(path, easy=False):
        return _fake_audio(duration=300.0, **TAGS_BY_STEM.get(Path(path).stem, {}))

    with patch("audiplex.scanners.music.mutagen.File", side_effect=by_path) as mock:
        yield mock


def _scan(db, root):
    result, _ = scan_music(db, str(root), str(root / "covers"))
    db.commit()
    return result


class TestFlatDumpGetsRealArtists:
    def test_root_folder_album_no_longer_yields_a_nameless_artist(
        self, db_session, rip_root, mocked_rip_tags
    ):
        _scan(db_session, rip_root)
        assert db_session.query(Artist).filter(Artist.name == "").count() == 0

    def test_high_confidence_parses_become_track_artists(
        self, db_session, rip_root, mocked_rip_tags
    ):
        _scan(db_session, rip_root)
        by_title = {t.title: t.artist.name for t in db_session.query(Track).all()}
        assert by_title["bury a friend"] == "Billie Eilish"
        assert by_title["Special"] == "Ashnikko"

    def test_low_confidence_is_flagged_and_not_written(
        self, db_session, rip_root, mocked_rip_tags
    ):
        """A wrong artist poisons identity keys; a blank one only limits them."""
        _scan(db_session, rip_root)
        repair = (
            db_session.query(TrackTagRepair)
            .filter(TrackTagRepair.proposed_title == "Barracuda")
            .one()
        )
        assert repair.confidence == LOW
        assert repair.status == PENDING_REVIEW
        assert repair.evidence

        track = db_session.query(Track).filter(Track.title == "Barracuda").one()
        assert track.artist.name == "Various Artists"  # unchanged, not guessed

    def test_every_file_gets_a_verdict_with_its_evidence(
        self, db_session, rip_root, mocked_rip_tags
    ):
        _scan(db_session, rip_root)
        repairs = db_session.query(TrackTagRepair).all()
        assert len(repairs) == 3
        assert all(r.evidence for r in repairs)
        assert all(r.source_url == YT for r in repairs)


class TestTasteDataSurvivesRescan:
    def _seed_history(self, db, track):
        db.add(PlayStat(track_id=track.id, event="start", played_seconds=0.0))
        db.add(PlayStat(track_id=track.id, event="complete", played_seconds=300.0))
        db.add(TrackRating(track_id=track.id, rating=5, note="a favourite"))
        db.commit()

    def test_play_stats_and_ratings_survive_a_changed_album(
        self, db_session, rip_root, mocked_rip_tags
    ):
        _scan(db_session, rip_root)
        track = db_session.query(Track).filter(Track.title == "Special").one()
        track_id = track.id
        self._seed_history(db_session, track)

        # One new file changes the album's hash — the exact trigger that used
        # to wipe the whole album's history.
        (rip_root / "Bill Withers - Ain't No Sunshine (128kbit_AAC).m4a").write_bytes(b"")
        TAGS_BY_STEM["Bill Withers - Ain't No Sunshine (128kbit_AAC)"] = {
            "title": "Bill Withers - Ain't No Sunshine",
            "artist": "BillWithersVEVO",
            "comment": YT,
        }
        try:
            result = _scan(db_session, rip_root)
            assert result.updated >= 1
        finally:
            del TAGS_BY_STEM["Bill Withers - Ain't No Sunshine (128kbit_AAC)"]

        assert db_session.query(PlayStat).filter(PlayStat.track_id == track_id).count() == 2
        assert db_session.query(TrackRating).filter(TrackRating.track_id == track_id).count() == 1

    def test_the_track_row_keeps_its_id_rather_than_being_replaced(
        self, db_session, rip_root, mocked_rip_tags
    ):
        """Identity is rebuilt from tags on every read, so nothing persists the
        recording key — but everything Todd has ever said about a song hangs off
        this integer. It has to be the same integer afterwards."""
        _scan(db_session, rip_root)
        before = {t.file_path: t.id for t in db_session.query(Track).all()}

        (rip_root / "spare.m4a").write_bytes(b"")
        _scan(db_session, rip_root)

        after = {t.file_path: t.id for t in db_session.query(Track).all()}
        for path, track_id in before.items():
            assert after[path] == track_id

    def test_a_deleted_file_still_removes_its_track(
        self, db_session, rip_root, mocked_rip_tags
    ):
        """Updating in place must not turn into never cleaning up."""
        _scan(db_session, rip_root)
        assert db_session.query(Track).count() == 3

        (rip_root / "Barracuda (128kbit_AAC).m4a").unlink()
        _scan(db_session, rip_root)

        assert db_session.query(Track).count() == 2
        assert db_session.query(Track).filter(Track.title == "Barracuda").count() == 0


class TestRepairsAreDurable:
    def test_an_applied_repair_is_reused_rather_than_recomputed(
        self, db_session, rip_root, mocked_rip_tags
    ):
        _scan(db_session, rip_root)
        repair = (
            db_session.query(TrackTagRepair)
            .filter(TrackTagRepair.proposed_title == "Barracuda")
            .one()
        )
        # Stand in for a verdict arriving from the RFL identification pass.
        repair.proposed_artist = "Heart"
        repair.confidence = HIGH
        repair.status = APPLIED
        repair.source = "rfl"
        db_session.commit()

        (rip_root / "spare.m4a").write_bytes(b"")
        _scan(db_session, rip_root)

        track = db_session.query(Track).filter(Track.title == "Barracuda").one()
        assert track.artist.name == "Heart"
        assert db_session.query(TrackTagRepair).filter(
            TrackTagRepair.proposed_title == "Barracuda"
        ).count() == 1

    def test_rescan_does_not_duplicate_verdicts(
        self, db_session, rip_root, mocked_rip_tags
    ):
        _scan(db_session, rip_root)
        _scan(db_session, rip_root)
        paths = [r.file_path for r in db_session.query(TrackTagRepair).all()]
        assert len(paths) == len(set(paths)) == 3

    def test_unchanged_album_with_verdicts_on_file_is_still_a_noop(
        self, db_session, rip_root, mocked_rip_tags
    ):
        _scan(db_session, rip_root)
        result = _scan(db_session, rip_root)
        assert (result.added, result.updated) == (0, 0)


class TestProperlyTaggedAlbumIsBelieved:
    def test_real_artist_tag_beats_the_path(self, db_session, tmp_path):
        """A folder named "<Artist> - <Album>" sitting loose under the root used
        to become "Various Artists" even when every file said otherwise."""
        root = tmp_path / "music"
        folder = root / "Marillion - Misplaced Childhood"
        folder.mkdir(parents=True)
        (folder / "02 Kayleigh.mp3").write_bytes(b"")

        def by_path(path, easy=False):
            return _fake_audio(duration=240.0, title="Kayleigh", artist="Marillion")

        with patch("audiplex.scanners.music.mutagen.File", side_effect=by_path):
            _scan(db_session, root)

        track = db_session.query(Track).one()
        assert track.artist.name == "Marillion"
        assert db_session.query(Album).one().title == "Marillion - Misplaced Childhood"
