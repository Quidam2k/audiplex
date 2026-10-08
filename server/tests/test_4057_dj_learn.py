"""The DJ learns from every ride (#4057): only Todd's skips move a weight."""

import json
import random
from datetime import datetime, timedelta, timezone

from audiplex import dj_learn, playback_bus
from audiplex.dj_pool import DJPool
from audiplex.models import Album, Artist, DjTrackWeight, PlayStat, Track, TrackRating, User
from audiplex.routers import dj_learn as dj_learn_router
from audiplex.routers import playback

T0 = datetime(2026, 10, 8, 18, 0, 0)  # naive UTC, like stored rows


def _tracks(db, n):
    artist = Artist(name="Kate Bush")
    db.add(artist)
    db.flush()
    album = Album(title="Hounds", artist_id=artist.id, genre="Rock", folder_path="/f")
    db.add(album)
    db.flush()
    out = []
    for i in range(n):
        t = Track(title=f"Song {i}", album_id=album.id, artist_id=artist.id, disc_number=1,
                  track_number=i + 1, duration_seconds=200, file_path=f"/f/{i}.mp3")
        db.add(t)
        out.append(t)
    db.commit()
    return [t.id for t in out]


def _owner(db):
    return db.query(User).first().id


def _stat(db, uid, tid, event, played, at):
    db.add(PlayStat(track_id=tid, user_id=uid, event=event, played_seconds=played, timestamp=at))
    db.commit()


def _ride(db, uid, ride, start, events, advances=()):
    for tid, event, played, offset in events:
        _stat(db, uid, tid, event, played, start + timedelta(seconds=offset))
    epochs = [(start + timedelta(seconds=s)).replace(tzinfo=timezone.utc).timestamp() for s in advances]
    return dj_learn.learn(db, uid, start, start + timedelta(hours=1), ride, advance_times=epochs)


def _w(db, tid):
    row = db.get(DjTrackWeight, tid)
    return row.weight if row else None


def test_todd_fast_skip_halves_and_dj_skip_is_ignored(db_session):
    a, b = _tracks(db_session, 2)
    uid = _owner(db_session)
    out = _ride(db_session, uid, "r1", T0,
                [(a, "skip", 2.0, 60), (b, "skip", 1.0, 300)], advances=[295])
    assert _w(db_session, a) == 0.5
    assert _w(db_session, b) is None
    assert (out["todd_skips"], out["agent_skips_ignored"]) == (1, 1)
    assert len(out["note"].splitlines()) == 3


def test_two_rides_drop_and_note_names_it_then_rating_restores(db_session):
    (a,) = _tracks(db_session, 1)
    uid = _owner(db_session)
    _ride(db_session, uid, "r1", T0, [(a, "skip", 1.0, 10)])
    out = _ride(db_session, uid, "r2", T0 + timedelta(days=1), [(a, "skip", 1.0, 10)])
    assert _w(db_session, a) == 0.0
    assert "Song 0 - Kate Bush" in out["note"].splitlines()[2]
    assert dj_learn.restore(db_session, a) is True
    assert _w(db_session, a) == 1.0


def test_played_through_and_loved(db_session):
    a, b = _tracks(db_session, 2)
    uid = _owner(db_session)
    db_session.add(TrackRating(user_id=uid, track_id=b, rating=5, updated_at=T0 + timedelta(minutes=5)))
    db_session.add(DjTrackWeight(track_id=b, weight=0.0, skip_rides=2))
    db_session.commit()
    out = _ride(db_session, uid, "r1", T0, [(a, "complete", 200, 200)])
    assert _w(db_session, a) == 1.1
    assert _w(db_session, b) == 1.3  # loved brings a dropped track back
    assert [e["track_id"] for e in out["up"]] == [a, b]


def test_replaying_a_ride_is_a_no_op(db_session):
    (a,) = _tracks(db_session, 1)
    uid = _owner(db_session)
    first = _ride(db_session, uid, "r1", T0, [(a, "skip", 1.0, 10)])
    again = dj_learn.learn(db_session, uid, T0, T0 + timedelta(hours=1), "r1", advance_times=[])
    assert again["already_learned"] is True and again["note"] == first["note"]
    assert _w(db_session, a) == 0.5


def test_diag_log_attribution_honours_by_todd(tmp_path, monkeypatch):
    log = tmp_path / "diag.jsonl"
    at = T0.replace(tzinfo=timezone.utc).timestamp()
    lines = [
        {"kind": "cmd_queued", "at": at + 10, "type": "skip", "payload": {}, "source": "jarvis:dj_skip"},
        {"kind": "cmd_queued", "at": at + 20, "type": "skip", "payload": {"by": "todd"}, "source": "jarvis:dj_skip"},
        {"kind": "cmd_queued", "at": at + 30, "type": "queue", "payload": {}, "source": "dj"},
    ]
    log.write_text("\n".join(map(json.dumps, lines)) + "\nnot json\n", encoding="utf-8")
    monkeypatch.setattr(playback_bus, "DIAG_LOG_PATH", log)
    assert dj_learn.agent_advance_times(T0, T0 + timedelta(minutes=5)) == [at + 10]


def test_weighted_pick_prefers_heavy():
    rng = random.Random(7)
    picks = [dj_learn.weighted_pick([1, 2], {1: 2.0, 2: 0.1}, rng) for _ in range(500)]
    assert picks.count(1) > 400


def test_pool_never_picks_a_dropped_track(db_session, tmp_path):
    a, b, c, d = _tracks(db_session, 4)
    db_session.add(DjTrackWeight(track_id=b, weight=0.0, skip_rides=2))
    db_session.commit()
    pool = DJPool(state_file=str(tmp_path / "pool.json"))
    pool.set_pool(spec_id=1, track_ids=[a, b, c, d], source_labels={a: "A", b: "A", c: "B", d: "B"}, ahead=3)
    out = pool.top_up(current_track_id=c, upcoming_track_ids=[], db=db_session)
    assert out["picks"] and b not in out["picks"], out
    assert pool.status()["dropped_by_learning"] == [b]


def test_learn_endpoint(client, db_engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    s = playback.get_settings().model_copy(update={"dj_owner_username": "testuser"})
    monkeypatch.setattr(playback, "get_settings", lambda: s)
    client.app.include_router(dj_learn_router.router)
    db = sessionmaker(bind=db_engine)()
    (a,) = _tracks(db, 1)
    _stat(db, _owner(db), a, "skip", 1.0, T0 + timedelta(seconds=10))
    monkeypatch.setattr(dj_learn, "agent_advance_times", lambda since, until: [])
    r = client.post("/api/dj/learn", json={"since": T0.isoformat(), "until": (T0 + timedelta(hours=1)).isoformat(),
                                           "ride_id": "api1"})
    assert r.status_code == 200, r.text
    assert r.json()["todd_skips"] == 1
    assert client.get("/api/dj/weights").json()[0] == {
        "track_id": a, "weight": 0.5, "reason": "fast-skipped by you (<5 s)", "ride_id": "api1", "skip_rides": 1}


def test_pause_book_bookmarks_for_the_owner_and_pauses(client, db_engine, sample_book, monkeypatch):
    """#4052: the DJ pauses a playing book and saves the owner's place first."""
    from sqlalchemy.orm import sessionmaker

    from audiplex.models import PlaybackPosition

    s = playback.get_settings().model_copy(update={"dj_owner_username": "testuser"})
    monkeypatch.setattr(playback, "get_settings", lambda: s)
    playback.bus.reset()
    assert client.post("/api/playback/pause-book", json={}).json()["paused"] is False

    playback.bus.set_state({"playing": True, "position_ms": 754_500,
                            "book": {"id": sample_book.id, "title": "B", "chapter_index": 3}})
    r = client.post("/api/playback/pause-book", json={"source": "jarvis:dj_play"}).json()
    assert r["paused"] is True and r["position_seconds"] == 754.5
    cmd = playback.bus.command(r["command_id"])
    assert (cmd.type, cmd.source) == ("pause", "jarvis:dj_play")
    db = sessionmaker(bind=db_engine)()
    pos = db.query(PlaybackPosition).filter_by(book_id=sample_book.id).one()
    assert (pos.position_seconds, pos.chapter_index) == (754.5, 3)
    playback.bus.reset()


def test_dj_love_tag_counts_as_loved_and_restores(client, db_engine):
    from sqlalchemy.orm import sessionmaker

    from audiplex.models import DjTrackTag

    db = sessionmaker(bind=db_engine)()
    a, b = _tracks(db, 2)
    db.add(DjTrackTag(track_id=a, tag="loved", created_at=T0 + timedelta(minutes=1)))
    db.add(DjTrackWeight(track_id=b, weight=0.0, skip_rides=2))
    db.commit()
    dj_learn.learn(db, _owner(db), T0, T0 + timedelta(hours=1), "r1", advance_times=[])
    assert _w(db, a) == 1.3
    assert client.post("/api/playback/tags", json={"track_ids": [b], "tags": ["loved"]}).status_code == 200
    db.expire_all()
    assert _w(db, b) == 1.0
