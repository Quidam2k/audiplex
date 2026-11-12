"""#5908: rating floor + playing-at tests."""

from datetime import datetime, timedelta, timezone

import pytest

from audiplex import dj_learn
from audiplex.models import (
    Album,
    Artist,
    PlayStat,
    RatingFloor,
    Track,
    TrackRating,
    User,
)
from audiplex.rating_floor import apply_floor, playing_at

T0 = datetime(2024, 1, 1, 12, 0, 0)  # naive UTC, like stored timestamps


@pytest.fixture
def restore_calls(monkeypatch):
    """Record dj_learn.restore calls instead of running them. #5908"""
    calls: list[int] = []

    def fake_restore(db, track_id):
        calls.append(track_id)
        return True

    monkeypatch.setattr(dj_learn, "restore", fake_restore)
    return calls


@pytest.fixture
def owner_id(db_session):
    return db_session.query(User).first().id


def _artist_album(db):
    artist = db.query(Artist).filter_by(name="Floor Artist").first()
    if artist is None:
        artist = Artist(name="Floor Artist")
        db.add(artist)
        db.flush()
    album = db.query(Album).filter_by(folder_path="/music/floor").first()
    if album is None:
        album = Album(title="Floor Album", artist_id=artist.id, folder_path="/music/floor")
        db.add(album)
        db.flush()
    return artist, album


def make_track(db, n, **kw):
    artist, album = _artist_album(db)
    track = Track(
        title=f"Track {n}",
        file_path=f"/music/floor/{n}.mp3",
        album_id=album.id,
        artist_id=artist.id,
        **kw,
    )
    db.add(track)
    db.commit()
    return track


def rate(db, owner_id, track_id, rating, note=None):
    row = TrackRating(
        user_id=owner_id,
        track_id=track_id,
        rating=rating,
        note=note or "",
        updated_at=T0,
    )
    db.add(row)
    db.commit()
    return row


def get_rating(db, owner_id, track_id):
    db.expire_all()
    return (
        db.query(TrackRating)
        .filter_by(user_id=owner_id, track_id=track_id)
        .first()
    )


def play(db, owner_id, track_id, event, ts):
    db.add(PlayStat(user_id=owner_id, track_id=track_id, event=event, timestamp=ts))
    db.commit()


# --- apply_floor ---


def test_unrated_track_is_raised(db_session, owner_id, restore_calls):
    t = make_track(db_session, 1)
    out = apply_floor(db_session, owner_id, [t.id], 4, "upload:1", "fresh upload", "DJ")

    assert out["results"] == [{"track_id": t.id, "outcome": "raised", "was": None, "now": 4}]
    assert out["unknown"] == []
    row = get_rating(db_session, owner_id, t.id)
    assert row.rating == 4
    assert row.note == "[DJ: fresh upload]"
    floor = db_session.query(RatingFloor).filter_by(track_id=t.id).one()
    assert floor.outcome == "raised"
    assert floor.old_rating is None
    assert floor.new_rating == 4
    assert floor.source_id == "upload:1"


def test_lower_rating_raised_and_note_kept(db_session, owner_id, restore_calls):
    t = make_track(db_session, 1)
    rate(db_session, owner_id, t.id, 3, note="good bassline")
    out = apply_floor(db_session, owner_id, [t.id], 4, "upload:1", "why", "DJ")

    assert out["results"][0]["outcome"] == "raised"
    assert out["results"][0]["was"] == 3
    row = get_rating(db_session, owner_id, t.id)
    assert row.rating == 4
    assert row.note == "good bassline [DJ: why]"


def test_never_lowers(db_session, owner_id, restore_calls):
    a = make_track(db_session, 1)
    b = make_track(db_session, 2)
    rate(db_session, owner_id, a.id, 4.5, note="keep me")
    rate(db_session, owner_id, b.id, 5)
    out = apply_floor(db_session, owner_id, [a.id, b.id], 4, "upload:1", "why", "DJ")

    assert [r["outcome"] for r in out["results"]] == ["kept", "kept"]
    ra = get_rating(db_session, owner_id, a.id)
    rb = get_rating(db_session, owner_id, b.id)
    assert ra.rating == 4.5
    assert ra.note == "keep me"
    assert rb.rating == 5
    assert db_session.query(RatingFloor).filter_by(outcome="kept").count() == 2


def test_same_source_twice_is_already(db_session, owner_id, restore_calls):
    t = make_track(db_session, 1)
    apply_floor(db_session, owner_id, [t.id], 4, "upload:1", "why", "DJ")
    out = apply_floor(db_session, owner_id, [t.id], 4, "upload:1", "why", "DJ")

    assert out["results"][0]["outcome"] == "already"
    assert db_session.query(RatingFloor).filter_by(track_id=t.id).count() == 1
    assert get_rating(db_session, owner_id, t.id).rating == 4


def test_manual_lowering_vetoes_later_floor(db_session, owner_id, restore_calls):
    t = make_track(db_session, 1)
    apply_floor(db_session, owner_id, [t.id], 4, "upload:1", "why", "DJ")

    row = get_rating(db_session, owner_id, t.id)
    row.rating = 2
    db_session.commit()

    out = apply_floor(db_session, owner_id, [t.id], 4, "upload:2", "again", "DJ")
    assert out["results"][0]["outcome"] == "vetoed"
    assert out["results"][0]["now"] == 2
    assert get_rating(db_session, owner_id, t.id).rating == 2
    vetoed = db_session.query(RatingFloor).filter_by(outcome="vetoed").one()
    assert vetoed.source_id == "upload:2"


def test_long_note_truncated_tag_whole(db_session, owner_id, restore_calls):
    t = make_track(db_session, 1)
    rate(db_session, owner_id, t.id, 3, note="x" * 490)
    apply_floor(db_session, owner_id, [t.id], 4, "upload:1", "reason", "DJ")

    note = get_rating(db_session, owner_id, t.id).note
    assert len(note) <= 500
    assert note.endswith("[DJ: reason]")
    assert note.startswith("x" * 100)


def test_unknown_track_id(db_session, owner_id, restore_calls):
    t = make_track(db_session, 1)
    out = apply_floor(db_session, owner_id, [t.id, 99999], 4, "upload:1", "why", "DJ")

    assert out["unknown"] == [99999]
    assert [r["track_id"] for r in out["results"]] == [t.id]


def test_restore_called_only_for_raised(db_session, owner_id, restore_calls):
    a = make_track(db_session, 1)
    b = make_track(db_session, 2)
    rate(db_session, owner_id, b.id, 5)
    stars = dj_learn.LOVE_MIN_STARS
    out = apply_floor(db_session, owner_id, [a.id, b.id], stars, "upload:1", "why", "DJ")

    assert restore_calls == [a.id]
    assert out["restored"] == [a.id]


# --- playing_at ---


def test_playing_at_within_and_after_skip(db_session, owner_id):
    t = make_track(db_session, 1, duration_seconds=300)
    play(db_session, owner_id, t.id, "start", T0)
    play(db_session, owner_id, t.id, "skip", T0 + timedelta(seconds=60))

    hit = playing_at(db_session, owner_id, T0 + timedelta(seconds=30))
    assert hit["track_id"] == t.id
    assert hit["title"] == "Track 1"
    assert hit["artist"] == "Floor Artist"
    assert hit["started_at"] == T0.isoformat()
    assert hit["ends_at"] == (T0 + timedelta(seconds=60)).isoformat()

    assert playing_at(db_session, owner_id, T0 + timedelta(seconds=90)) is None


def test_playing_at_falls_back_to_duration(db_session, owner_id):
    t = make_track(db_session, 1, duration_seconds=100)
    play(db_session, owner_id, t.id, "start", T0)

    hit = playing_at(db_session, owner_id, T0 + timedelta(seconds=50))
    assert hit["track_id"] == t.id
    assert hit["ends_at"] == (T0 + timedelta(seconds=100)).isoformat()
    assert playing_at(db_session, owner_id, T0 + timedelta(seconds=150)) is None


def test_playing_at_end_trim_beats_duration(db_session, owner_id):
    t = make_track(db_session, 1, duration_seconds=200, end_trim_seconds=30)
    play(db_session, owner_id, t.id, "start", T0)

    assert playing_at(db_session, owner_id, T0 + timedelta(seconds=20))["track_id"] == t.id
    assert playing_at(db_session, owner_id, T0 + timedelta(seconds=40)) is None


def test_playing_at_older_than_lookback(db_session, owner_id):
    t = make_track(db_session, 1, duration_seconds=100000)
    play(db_session, owner_id, t.id, "start", T0)

    assert playing_at(db_session, owner_id, T0 + timedelta(hours=1))["track_id"] == t.id
    assert playing_at(db_session, owner_id, T0 + timedelta(hours=5)) is None


def test_playing_at_aware_datetime(db_session, owner_id):
    t = make_track(db_session, 1, duration_seconds=300)
    play(db_session, owner_id, t.id, "start", T0)

    # 07:00:30 at UTC-5 is 12:00:30 UTC
    at = datetime(2024, 1, 1, 7, 0, 30, tzinfo=timezone(timedelta(hours=-5)))
    hit = playing_at(db_session, owner_id, at)
    assert hit["track_id"] == t.id


def test_playing_at_no_events(db_session, owner_id):
    assert playing_at(db_session, owner_id, T0) is None


# --- routes (#5908): owner-scoped like /ratings; the DJ calls as dj-agent ---


@pytest.fixture
def agent(client, db_session, monkeypatch):
    from fastapi import Depends

    from audiplex.auth import get_current_user, hash_password
    from audiplex.config import get_settings
    from audiplex.database import get_db

    owner = db_session.query(User).first()
    monkeypatch.setattr(get_settings(), "dj_owner_username", owner.username)
    db_session.add(User(username="dj-agent", password_hash=hash_password("x"), display_name="DJ", is_admin=False))
    db_session.commit()
    client.app.dependency_overrides[get_current_user] = (
        lambda db=Depends(get_db): db.query(User).filter(User.username == "dj-agent").first())
    yield client, owner.id
    client.app.dependency_overrides[get_current_user] = lambda db=Depends(get_db): db.query(User).first()


def test_floor_route_rates_the_owner_and_logs_it(agent, db_session, restore_calls):
    client, owner = agent
    t = make_track(db_session, 1)
    rate(db_session, owner, t.id, 3)
    r = client.put("/api/playback/ratings/floor", json={"track_ids": [t.id], "source_id": "upload:6339", "persona": "Orolo"})
    assert r.status_code == 200
    assert r.json()["results"][0] == {"track_id": t.id, "outcome": "raised", "was": 3, "now": 4}
    assert get_rating(db_session, owner, t.id).note == "[Orolo: sang along in a dead drop]"
    log = client.get("/api/playback/ratings/floors").json()
    assert log[0]["source_id"] == "upload:6339" and log[0]["old"] == 3 and log[0]["new"] == 4


def test_floor_route_wants_halves(agent):
    client, _ = agent
    r = client.put("/api/playback/ratings/floor", json={"track_ids": [1], "source_id": "u:1", "min_stars": 3.7})
    assert r.status_code == 422


def test_playing_at_route_takes_z_times(agent, db_session):
    client, owner = agent
    t = make_track(db_session, 1, duration_seconds=300)
    play(db_session, owner, t.id, "start", T0)
    assert client.get("/api/playback/playing-at", params={"at": "2024-01-01T12:01:00Z"}).json()["track"]["track_id"] == t.id
    assert client.get("/api/playback/playing-at", params={"at": "2024-01-01T13:00:00Z"}).json() == {"track": None}
    assert client.get("/api/playback/playing-at", params={"at": "noon"}).status_code == 422


def test_a_higher_floor_still_raises_an_untouched_lower_floor(db_session, owner_id, restore_calls):
    # #5908 review: only a rating he moved below what a floor set is a veto
    t = make_track(db_session, 1)
    apply_floor(db_session, owner_id, [t.id], 3, "upload:1", "why", "DJ")
    out = apply_floor(db_session, owner_id, [t.id], 4, "upload:2", "why", "DJ")
    assert out["results"][0]["outcome"] == "raised"
    assert get_rating(db_session, owner_id, t.id).rating == 4


def test_same_second_skip_ends_the_window(db_session, owner_id):
    t = make_track(db_session, 1, duration_seconds=300)
    play(db_session, owner_id, t.id, "start", T0)
    play(db_session, owner_id, t.id, "skip", T0)
    assert playing_at(db_session, owner_id, T0 + timedelta(seconds=30)) is None


def test_restore_failure_does_not_lose_the_ratings(db_session, owner_id, monkeypatch):
    def boom(db, tid):
        raise RuntimeError("x")

    monkeypatch.setattr(dj_learn, "restore", boom)
    t = make_track(db_session, 1)
    out = apply_floor(db_session, owner_id, [t.id], 4, "upload:1", "why", "DJ")
    assert out["restored"] == [] and get_rating(db_session, owner_id, t.id).rating == 4
