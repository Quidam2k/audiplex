"""Importing RFL identification verdicts (#3037 Phase 4).

The rule under test is that an external identification is only as good as its
corroboration. MusicBrainz matches largely on TITLE, so a lookup for "stars"
returns a confident hit for somebody's recording called "stars" — which is how
the first batch identified an Arcane soundtrack cut as a Suzy Bogguss country
song at 0.823. Both of those live cases are here.
"""

import json

import pytest

from audiplex import rfl_import
from audiplex.models import TrackTagRepair
from audiplex.tag_repair import APPLIED, HIGH, LOW, MEDIUM, PENDING_REVIEW


def _repair(db, path, **kwargs):
    row = TrackTagRepair(
        file_path=path,
        confidence=kwargs.pop("confidence", LOW),
        status=kwargs.pop("status", PENDING_REVIEW),
        source=kwargs.pop("source", "parser"),
        evidence="parser verdict",
        **kwargs,
    )
    db.add(row)
    db.commit()
    return row


def _verdict(path, **kwargs):
    base = {
        "track_id": 1,
        "file_path": path,
        "verdict": "identified",
        "artist": "Somebody",
        "title": "A Song",
        "confidence": MEDIUM,
        "evidence": "MusicBrainz [confidence 0.823]",
        "source": "rfl",
    }
    base.update(kwargs)
    return base


class TestCorroboration:
    def test_artist_named_in_the_title_tag_counts(self):
        assert rfl_import.corroborated(
            "Anna Kendrick", "Cups Pitch Perfect's When I'm Gone - Anna Kendrick", None
        )

    def test_matching_channel_counts(self):
        assert rfl_import.corroborated("Billie Eilish", "bury a friend", "BillieEilishVEVO")

    def test_nothing_in_the_file_saying_so_does_not(self):
        """The Suzy Bogguss case: a confident title match on a track whose own
        tags never mention her."""
        assert not rfl_import.corroborated(
            "Suzy Bogguss", 'Arcane: Season 2 | "Paint The Town Blue"', "Netflix"
        )

    def test_a_two_letter_name_cannot_corroborate_by_accident(self):
        assert not rfl_import.corroborated("XY", "Anything At All", None)

    def test_no_artist_is_never_corroborated(self):
        assert not rfl_import.corroborated(None, "A Song", "A Channel")


class TestImport:
    def test_high_confidence_applies_without_corroboration(self, db_session):
        _repair(db_session, "/music/a.m4a")
        counts, _ = rfl_import.import_verdicts(
            db_session,
            [_verdict("/music/a.m4a", confidence=HIGH, artist="Jesse Welles",
                      title="Good Morning America")],
        )
        assert counts.applied == 1
        row = db_session.query(TrackTagRepair).one()
        assert (row.proposed_artist, row.status, row.source) == (
            "Jesse Welles", APPLIED, "rfl",
        )
        assert "MusicBrainz" in row.evidence

    def test_uncorroborated_medium_is_held_but_kept(self, db_session):
        _repair(db_session, "/music/b.m4a", proposed_title="Paint The Town Blue")
        counts, _ = rfl_import.import_verdicts(
            db_session, [_verdict("/music/b.m4a", artist="Suzy Bogguss")]
        )
        assert counts.held_uncorroborated == 1
        row = db_session.query(TrackTagRepair).one()
        assert row.status == PENDING_REVIEW
        # Held, not discarded — the queue carries what RFL found and why it lost.
        assert row.proposed_artist == "Suzy Bogguss"
        assert "HELD" in row.evidence

    def test_low_confidence_is_held(self, db_session):
        _repair(db_session, "/music/c.m4a")
        counts, _ = rfl_import.import_verdicts(
            db_session, [_verdict("/music/c.m4a", confidence=LOW)]
        )
        assert (counts.held_low, counts.applied) == (1, 0)

    def test_unidentified_is_held(self, db_session):
        _repair(db_session, "/music/d.m4a")
        counts, _ = rfl_import.import_verdicts(
            db_session, [_verdict("/music/d.m4a", verdict="unidentified", artist=None)]
        )
        assert counts.held_unidentified == 1
        assert db_session.query(TrackTagRepair).one().status == PENDING_REVIEW

    def test_non_music_is_held_and_says_which_ticket_owns_it(self, db_session):
        _repair(db_session, "/music/e.m4a")
        counts, _ = rfl_import.import_verdicts(
            db_session, [_verdict("/music/e.m4a", verdict="non_music")]
        )
        assert counts.held_non_music == 1
        assert "#954" in db_session.query(TrackTagRepair).one().evidence

    def test_an_already_applied_repair_is_never_clobbered(self, db_session):
        """Folder consensus settled the Marillion tracks before RFL ran. An
        import must not reopen or overwrite a resolved row."""
        _repair(
            db_session,
            "/music/f.m4a",
            status=APPLIED,
            source="consensus",
            confidence=HIGH,
            proposed_artist="Marillion",
            proposed_title="Pseudo Silk Kimono",
        )
        counts, _ = rfl_import.import_verdicts(
            db_session, [_verdict("/music/f.m4a", artist="Fish", title="Pseudo Silk Kimono")]
        )
        assert counts.already_resolved == 1
        row = db_session.query(TrackTagRepair).one()
        assert (row.proposed_artist, row.source) == ("Marillion", "consensus")

    def test_a_verdict_for_an_unknown_file_is_counted_not_crashed(self, db_session):
        counts, _ = rfl_import.import_verdicts(db_session, [_verdict("/music/gone.m4a")])
        assert counts.unknown_file == 1

    def test_every_verdict_is_accounted_for(self, db_session):
        for name in "abcde":
            _repair(db_session, f"/music/{name}.m4a")
        rows = [
            _verdict("/music/a.m4a", confidence=HIGH),
            _verdict("/music/b.m4a"),
            _verdict("/music/c.m4a", confidence=LOW),
            _verdict("/music/d.m4a", verdict="unidentified"),
            _verdict("/music/e.m4a", verdict="non_music"),
        ]
        counts, log = rfl_import.import_verdicts(db_session, rows)
        assert sum(counts.as_dict().values()) == len(rows)
        assert len(log) == len(rows)


class TestLoad:
    def test_reads_the_verdict_file(self, tmp_path):
        path = tmp_path / "verdicts.json"
        path.write_text(json.dumps([_verdict("/music/a.m4a")]), encoding="utf-8")
        assert rfl_import.load(str(path))[0]["artist"] == "Somebody"
