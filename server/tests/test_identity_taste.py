"""Song identity, taste aggregation, recency cooldown (#943/#947/#948).

The unit tests below build rows directly rather than going through the API,
because what is being tested is a JUDGEMENT — is this the same recording, is
this the same song — and the interesting cases (a remaster, a live cut, two
rips of one file) are ones no fixture happens to contain.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends
from sqlalchemy.orm import sessionmaker

from audiplex.auth import get_current_user, hash_password
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.identity import (
    build_identity_map,
    normalize,
    strip_qualifiers,
    work_key,
)
from audiplex.models import Album, Artist, PlayStat, Track, TrackRating, User
from audiplex.taste import (
    filter_candidates,
    recent_plays_for,
    recording_stats_for,
)


def _add_track(db, artist, title, duration, *, file_hash=None, album=None):
    """One track under `artist`, creating the artist/album rows on demand."""
    artist_row = db.query(Artist).filter(Artist.name == artist).first()
    if artist_row is None:
        artist_row = Artist(name=artist)
        db.add(artist_row)
        db.flush()

    album_title = album or "Test Album"
    album_row = (
        db.query(Album)
        .filter(Album.title == album_title, Album.artist_id == artist_row.id)
        .first()
    )
    if album_row is None:
        album_row = Album(
            title=album_title,
            artist_id=artist_row.id,
            genre="Rock",
            folder_path=f"/fake/{artist}/{album_title}",
        )
        db.add(album_row)
        db.flush()

    track = Track(
        title=title,
        album_id=album_row.id,
        artist_id=artist_row.id,
        disc_number=1,
        track_number=db.query(Track).count() + 1,
        duration_seconds=duration,
        file_path=f"/fake/{artist}/{album_title}/{title}-{duration}.mp3",
        file_hash=file_hash,
    )
    db.add(track)
    db.commit()
    db.refresh(track)
    return track


def _play(db, user_id, track_id, event, played_seconds=0.0, minutes_ago=0.0):
    stat = PlayStat(
        track_id=track_id,
        user_id=user_id,
        event=event,
        played_seconds=played_seconds,
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    db.add(stat)
    db.commit()
    return stat


class TestNormalization:
    def test_folds_case_accents_and_punctuation(self):
        assert normalize("Björk & Friends!") == normalize("bjork and friends")

    def test_drops_leading_article_so_the_band_is_the_band(self):
        assert normalize("The Beatles") == normalize("Beatles")

    def test_strips_bracketed_qualifiers(self):
        assert strip_qualifiers("Barracuda (2004 Remaster)") == "barracuda"
        assert strip_qualifiers("Barracuda [Live at Budokan]") == "barracuda"

    def test_strips_dash_qualifier_only_when_it_marks_a_version(self):
        assert strip_qualifiers("Barracuda - 2004 Remaster") == "barracuda"
        # Not a version marker — the dash is just punctuation in the title.
        assert strip_qualifiers("Life - A Portrait") == "life a portrait"

    def test_a_title_that_is_only_a_qualifier_keeps_its_name(self):
        """Otherwise every '(Untitled)' in the library collapses into one song."""
        assert strip_qualifiers("(Untitled)") == "untitled"

    def test_same_song_different_artists_are_different_works(self):
        assert work_key("Hurt", "Nine Inch Nails") != work_key("Hurt", "Johnny Cash")


class TestRecordingIdentity:
    def test_two_rips_within_tolerance_are_one_recording(self, db_session):
        a = _add_track(db_session, "Heart", "Barracuda", 260.0)
        b = _add_track(db_session, "Heart", "Barracuda", 262.0, album="Greatest Hits")
        ids = build_identity_map(db_session)
        assert ids[a.id].recording_id == ids[b.id].recording_id

    def test_durations_beyond_tolerance_stay_separate(self, db_session):
        a = _add_track(db_session, "Heart", "Barracuda", 260.0)
        b = _add_track(db_session, "Heart", "Barracuda", 400.0, album="Extended")
        ids = build_identity_map(db_session)
        assert ids[a.id].recording_id != ids[b.id].recording_id

    def test_identical_file_hash_wins_over_everything_else(self, db_session):
        """Same bytes is the strongest evidence there is — tags can disagree."""
        a = _add_track(db_session, "Heart", "Barracuda", 260.0, file_hash="deadbeef")
        b = _add_track(
            db_session,
            "Heart",
            "Barracuda (2004 Remaster)",
            999.0,
            file_hash="deadbeef",
            album="Remasters",
        )
        ids = build_identity_map(db_session)
        assert ids[a.id].recording_id == ids[b.id].recording_id

    def test_live_and_studio_share_a_work_but_not_a_recording(self, db_session):
        """The whole reason there are two keys.

        Cooldown must treat these as one song. Ratings must not: liking the
        studio cut and finding the live one thin is a real opinion, and one
        key would average it away.
        """
        studio = _add_track(db_session, "Heart", "Barracuda", 260.0)
        live = _add_track(
            db_session, "Heart", "Barracuda (Live)", 295.0, album="Live In Seattle"
        )
        ids = build_identity_map(db_session)
        assert ids[studio.id].work_id == ids[live.id].work_id
        assert ids[studio.id].recording_id != ids[live.id].recording_id

    def test_recording_label_survives_a_new_copy_appearing(self, db_session):
        """A recording's name must not change when #943 syncs a phone copy —
        ratings are bound to it."""
        first = _add_track(db_session, "Heart", "Barracuda", 260.0)
        before = build_identity_map(db_session)[first.id].recording_id
        _add_track(db_session, "Heart", "Barracuda", 261.0, album="On The Phone")
        assert build_identity_map(db_session)[first.id].recording_id == before


class TestRecordingStats:
    def test_completion_rate_separates_finished_from_merely_started(self, db_session):
        user = db_session.query(User).first()
        loved = _add_track(db_session, "Heart", "Barracuda", 260.0)
        endured = _add_track(db_session, "Heart", "Dog & Butterfly", 300.0)

        for _ in range(2):
            _play(db_session, user.id, loved.id, "start")
            _play(db_session, user.id, loved.id, "complete", 260.0)
        for _ in range(8):
            _play(db_session, user.id, endured.id, "start")
        for _ in range(2):
            _play(db_session, user.id, endured.id, "complete", 300.0)

        stats = recording_stats_for(db_session, user.id)
        by_track = {s.track_ids[0]: s for s in stats.values()}
        # Same two completes each — the rate is what tells them apart.
        assert by_track[loved.id].completes == by_track[endured.id].completes == 2
        assert by_track[loved.id].completion_rate == 1.0
        assert by_track[endured.id].completion_rate == 0.25

    def test_never_started_has_no_rate_rather_than_a_zero(self, db_session):
        """0.0 would read as 'he never finishes it'. Null means 'no evidence'."""
        user = db_session.query(User).first()
        track = _add_track(db_session, "Heart", "Barracuda", 260.0)
        stats = recording_stats_for(db_session, user.id)
        entry = next(s for s in stats.values() if track.id in s.track_ids)
        assert entry.completion_rate is None

    def test_skip_positions_report_where_it_loses_him(self, db_session):
        user = db_session.query(User).first()
        track = _add_track(db_session, "Heart", "Barracuda", 260.0)
        for position in (4.0, 6.0, 120.0):
            _play(db_session, user.id, track.id, "skip", position)

        entry = next(
            s for s in recording_stats_for(db_session, user.id).values()
            if track.id in s.track_ids
        )
        assert entry.abandons == 3
        assert entry.early_skips == 2  # under the 10s threshold
        assert entry.median_skip_seconds == 6.0
        assert entry.mean_skip_seconds == pytest.approx(43.33, abs=0.01)

    def test_two_copies_of_one_recording_pool_their_history(self, db_session):
        """Otherwise a track that exists locally AND on the server looks
        half-listened-to twice (#943)."""
        user = db_session.query(User).first()
        served = _add_track(db_session, "Heart", "Barracuda", 260.0, file_hash="beef")
        on_phone = _add_track(
            db_session, "Heart", "Barracuda", 260.0, file_hash="beef", album="Phone"
        )
        _play(db_session, user.id, served.id, "start")
        _play(db_session, user.id, served.id, "complete", 260.0)
        _play(db_session, user.id, on_phone.id, "start")
        _play(db_session, user.id, on_phone.id, "complete", 260.0)

        stats = recording_stats_for(db_session, user.id)
        entry = next(s for s in stats.values() if served.id in s.track_ids)
        assert sorted(entry.track_ids) == sorted([served.id, on_phone.id])
        assert entry.starts == 2 and entry.completes == 2

    def test_another_users_listening_is_not_counted(self, db_session):
        user = db_session.query(User).first()
        other = User(
            username="someone-else",
            password_hash=hash_password("x"),
            display_name="Someone Else",
        )
        db_session.add(other)
        db_session.commit()
        track = _add_track(db_session, "Heart", "Barracuda", 260.0)
        _play(db_session, other.id, track.id, "start")

        entry = next(
            s for s in recording_stats_for(db_session, user.id).values()
            if track.id in s.track_ids
        )
        assert entry.starts == 0


class TestCooldown:
    def test_recent_plays_respect_the_window(self, db_session):
        user = db_session.query(User).first()
        track = _add_track(db_session, "Heart", "Barracuda", 260.0)
        _play(db_session, user.id, track.id, "start", minutes_ago=5)
        _play(db_session, user.id, track.id, "start", minutes_ago=90)

        assert len(recent_plays_for(db_session, user.id, 20)) == 1
        assert len(recent_plays_for(db_session, user.id, 120)) == 2

    def test_the_same_recording_is_suppressed_with_a_reason(self, db_session):
        user = db_session.query(User).first()
        track = _add_track(db_session, "Heart", "Barracuda", 260.0)
        _play(db_session, user.id, track.id, "complete", 260.0, minutes_ago=5)

        verdict = filter_candidates(db_session, user.id, [track.id], 20, 20)
        assert verdict.allowed == []
        assert verdict.suppressed[0].reason == "recording_cooldown"
        assert "5 min ago" in verdict.suppressed[0].detail
        assert verdict.suppressed[0].clears_in_minutes == pytest.approx(15, abs=0.1)

    def test_a_different_version_is_suppressed_at_work_level(self, db_session):
        """Todd doesn't want the song twice in twenty minutes in ANY form."""
        user = db_session.query(User).first()
        studio = _add_track(db_session, "Heart", "Barracuda", 260.0)
        live = _add_track(
            db_session, "Heart", "Barracuda (Live)", 295.0, album="Live In Seattle"
        )
        _play(db_session, user.id, studio.id, "complete", 260.0, minutes_ago=3)

        verdict = filter_candidates(db_session, user.id, [live.id], 20, 20)
        assert verdict.suppressed[0].reason == "work_cooldown"
        assert "another version" in verdict.suppressed[0].detail

    def test_an_early_skip_still_counts_as_recently_heard(self, db_session):
        """He heard the front of it two minutes ago AND disliked it. Both are
        reasons not to play it again right now."""
        user = db_session.query(User).first()
        track = _add_track(db_session, "Heart", "Barracuda", 260.0)
        _play(db_session, user.id, track.id, "skip", 4.0, minutes_ago=2)

        verdict = filter_candidates(db_session, user.id, [track.id], 20, 20)
        assert verdict.allowed == []

    def test_outside_the_window_it_is_playable_again(self, db_session):
        user = db_session.query(User).first()
        track = _add_track(db_session, "Heart", "Barracuda", 260.0)
        _play(db_session, user.id, track.id, "complete", 260.0, minutes_ago=45)

        verdict = filter_candidates(db_session, user.id, [track.id], 20, 20)
        assert verdict.allowed == [track.id]
        assert verdict.suppressed == []

    def test_cooldowns_are_tunable_independently(self, db_session):
        user = db_session.query(User).first()
        studio = _add_track(db_session, "Heart", "Barracuda", 260.0)
        live = _add_track(
            db_session, "Heart", "Barracuda (Live)", 295.0, album="Live In Seattle"
        )
        _play(db_session, user.id, studio.id, "complete", 260.0, minutes_ago=30)

        # Work cooldown of an hour still catches the live version; the
        # recording cooldown alone would have released it.
        verdict = filter_candidates(db_session, user.id, [live.id], 20, 60)
        assert verdict.suppressed[0].reason == "work_cooldown"

    def test_low_rating_suppression_is_opt_in(self, db_session):
        user = db_session.query(User).first()
        track = _add_track(db_session, "Heart", "Barracuda", 260.0)
        db_session.add(TrackRating(user_id=user.id, track_id=track.id, rating=2))
        db_session.commit()

        assert filter_candidates(db_session, user.id, [track.id], 20, 20).allowed == [
            track.id
        ]
        opted_in = filter_candidates(
            db_session, user.id, [track.id], 20, 20, min_rating=3
        )
        assert opted_in.suppressed[0].reason == "low_rating"

    def test_an_unknown_track_is_not_ours_to_veto(self, db_session):
        user = db_session.query(User).first()
        verdict = filter_candidates(db_session, user.id, [424242], 20, 20)
        assert verdict.allowed == [424242]


class TestIdentityAndTasteEndpoints:
    def test_track_identity_lists_what_shares_its_keys(self, client, db_session):
        studio = _add_track(db_session, "Heart", "Barracuda", 260.0)
        live = _add_track(
            db_session, "Heart", "Barracuda (Live)", 295.0, album="Live In Seattle"
        )
        resp = client.get(f"/api/music/tracks/{studio.id}/identity")
        assert resp.status_code == 200
        data = resp.json()
        assert data["same_recording_track_ids"] == [studio.id]
        assert data["same_work_track_ids"] == sorted([studio.id, live.id])

    def test_track_identity_404s_for_a_missing_track(self, client):
        assert client.get("/api/music/tracks/999999/identity").status_code == 404

    def test_track_stats_endpoint_reports_the_rate(self, client, db_session):
        user = db_session.query(User).first()
        track = _add_track(db_session, "Heart", "Barracuda", 260.0)
        for _ in range(4):
            _play(db_session, user.id, track.id, "start")
        _play(db_session, user.id, track.id, "complete", 260.0)

        resp = client.get("/api/music/track-stats")
        assert resp.status_code == 200
        entry = next(e for e in resp.json() if track.id in e["track_ids"])
        assert entry["starts"] == 4
        assert entry["completion_rate"] == 0.25
        assert entry["track"]["title"] == "Barracuda"


class TestOwnerScopedTasteReads:
    """A service account has no listening history of its own, so a per-caller
    read hands the DJ an empty world forever (#3028)."""

    @pytest.fixture
    def dj_agent_client(self, client, db_session, monkeypatch):
        monkeypatch.setattr(get_settings(), "dj_owner_username", "testuser")
        db_session.add(
            User(
                username="dj-agent",
                password_hash=hash_password("unused"),
                display_name="DJ Agent",
                is_admin=False,
            )
        )
        db_session.commit()

        def override_as_agent(db=Depends(get_db)):
            return db.query(User).filter(User.username == "dj-agent").first()

        client.app.dependency_overrides[get_current_user] = override_as_agent
        yield client
        client.app.dependency_overrides[get_current_user] = (
            lambda db=Depends(get_db): db.query(User).first()
        )

    def _owner(self, db_session):
        return db_session.query(User).filter(User.username == "testuser").first()

    def test_dj_sees_the_owners_completion_rates(self, dj_agent_client, db_session):
        owner = self._owner(db_session)
        track = _add_track(db_session, "Heart", "Barracuda", 260.0)
        _play(db_session, owner.id, track.id, "start")
        _play(db_session, owner.id, track.id, "complete", 260.0)

        resp = dj_agent_client.get("/api/playback/track-stats")
        assert resp.status_code == 200
        assert any(track.id in e["track_ids"] for e in resp.json())

    def test_dj_sees_the_owners_recent_plays(self, dj_agent_client, db_session):
        owner = self._owner(db_session)
        track = _add_track(db_session, "Heart", "Barracuda", 260.0)
        _play(db_session, owner.id, track.id, "complete", 260.0, minutes_ago=5)

        resp = dj_agent_client.get("/api/playback/cooldown")
        assert resp.status_code == 200
        data = resp.json()
        assert data["recording_cooldown_minutes"] == 20
        assert data["recent_plays"][0]["track_id"] == track.id
        assert data["recent_plays"][0]["title"] == "Barracuda"

    def test_candidate_filter_explains_what_it_passed_over(
        self, dj_agent_client, db_session
    ):
        owner = self._owner(db_session)
        played = _add_track(db_session, "Heart", "Barracuda", 260.0)
        fresh = _add_track(db_session, "Heart", "Crazy On You", 290.0)
        _play(db_session, owner.id, played.id, "complete", 260.0, minutes_ago=2)

        resp = dj_agent_client.post(
            "/api/playback/candidates/filter",
            json={"track_ids": [played.id, fresh.id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed"] == [fresh.id]
        assert data["suppressed"][0]["track_id"] == played.id
        assert data["suppressed"][0]["reason"] == "recording_cooldown"
        # The reason travels with it — a silently shortened list teaches the
        # DJ nothing and leaves Todd wondering.
        assert data["suppressed"][0]["title"] == "Barracuda"

    def test_the_window_is_tunable_per_request(self, dj_agent_client, db_session):
        owner = self._owner(db_session)
        track = _add_track(db_session, "Heart", "Barracuda", 260.0)
        _play(db_session, owner.id, track.id, "complete", 260.0, minutes_ago=25)

        default_window = dj_agent_client.post(
            "/api/playback/candidates/filter", json={"track_ids": [track.id]}
        ).json()
        assert default_window["allowed"] == [track.id]

        longer = dj_agent_client.post(
            "/api/playback/candidates/filter",
            json={
                "track_ids": [track.id],
                "recording_cooldown_minutes": 60,
                "work_cooldown_minutes": 60,
            },
        ).json()
        assert longer["allowed"] == []
        assert longer["recording_cooldown_minutes"] == 60
