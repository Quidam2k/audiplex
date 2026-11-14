"""#6199 (Todd msg 61546): skip ladder 75% -> 50% -> 25% -> never, a 15 s fast-skip window
for UNRATED whole-album tracks, rated songs floor at 25%, and the ride summary lists every
skipped track so the Pantheon ride outro never names one."""

import json
from datetime import datetime, timedelta, timezone

from audiplex import dj_learn
from audiplex.models import Album, Artist, DjTrackWeight, PlayStat, Track, TrackRating, User

T0 = datetime(2026, 11, 14, 19, 0, 0)  # naive UTC, like stored rows


def _tracks(db, n):
    artist = Artist(name="Cake")
    db.add(artist)
    db.flush()
    album = Album(title="Fashion Nugget", artist_id=artist.id, genre="Rock", folder_path="/c")
    db.add(album)
    db.flush()
    out = []
    for i in range(n):
        t = Track(title=f"Song {i}", album_id=album.id, artist_id=artist.id, disc_number=1,
                  track_number=i + 1, duration_seconds=200, file_path=f"/c/{i}.mp3")
        db.add(t)
        out.append(t)
    db.commit()
    return [t.id for t in out]


def _uid(db):
    return db.query(User).first().id


def _ride(db, n, events, whole=(), advances=()):
    """Ride number n (one day apart). events: (track_id, event, played_seconds, offset_s)."""
    start = T0 + timedelta(days=n)
    uid = _uid(db)
    for tid, event, played, offset in events:
        db.add(PlayStat(track_id=tid, user_id=uid, event=event, played_seconds=played,
                        timestamp=start + timedelta(seconds=offset)))
    db.commit()
    epochs = [(start + timedelta(seconds=s)).replace(tzinfo=timezone.utc).timestamp() for s in advances]
    return dj_learn.learn(db, uid, start, start + timedelta(hours=1), f"r{n}",
                          advance_times=epochs, whole_album_ids=set(whole))


def _row(db, tid):
    db.expire_all()
    return db.get(DjTrackWeight, tid)


def test_ladder_steps_75_50_25_then_drop(db_session):
    (a,) = _tracks(db_session, 1)
    seen = []
    for n in range(4):
        out = _ride(db_session, n, [(a, "skip", 2.0, 10)])
        seen.append(_row(db_session, a).weight)
    assert seen == [0.75, 0.5, 0.25, 0.0]
    assert [e["track_id"] for e in out["dropped"]] == [a]
    assert _row(db_session, a).skip_rides == 4


def test_fifteen_second_window_only_for_unrated_whole_album_tracks(db_session):
    whole_unrated, whole_rated, elsewhere = _tracks(db_session, 3)
    db_session.add(TrackRating(user_id=_uid(db_session), track_id=whole_rated, rating=3))
    db_session.commit()
    _ride(db_session, 0, [(whole_unrated, "skip", 10.0, 10), (whole_rated, "skip", 10.0, 300),
                          (elsewhere, "skip", 10.0, 600)], whole={whole_unrated, whole_rated})
    assert _row(db_session, whole_unrated).weight == 0.75
    assert _row(db_session, whole_rated) is None  # rated: the 5 s line still applies
    assert _row(db_session, elsewhere) is None  # not whole albums: 5 s line
    _ride(db_session, 1, [(whole_unrated, "skip", 16.0, 10)], whole={whole_unrated})
    assert _row(db_session, whole_unrated).weight == 0.75  # 16 s is past the 15 s window


def test_rated_song_floors_at_25_and_a_4_star_rating_restores_it(db_session):
    (a,) = _tracks(db_session, 1)
    db_session.add(TrackRating(user_id=_uid(db_session), track_id=a, rating=3))
    db_session.commit()
    for n in range(5):
        out = _ride(db_session, n, [(a, "skip", 1.0, 10)])
    assert _row(db_session, a).weight == 0.25
    assert out["dropped"] == []
    assert dj_learn.restore(db_session, a) is True
    row = _row(db_session, a)
    assert (row.weight, row.skip_rides) == (1.0, 0)
    assert dj_learn.restore(db_session, a) is False  # nothing left to restore


def test_legacy_halved_rows_map_to_the_50_step_once(db_session):
    a, b = _tracks(db_session, 2)
    db_session.add(DjTrackWeight(track_id=a, weight=0.5, skip_rides=1, reason="fast-skipped by you (<5 s)"))
    db_session.add(DjTrackWeight(track_id=b, weight=0.0, skip_rides=2,
                                 reason="dropped: you skipped it on 2 rides"))
    db_session.commit()
    _ride(db_session, 0, [])
    _ride(db_session, 1, [])  # a second pass must not step it again
    row = _row(db_session, a)
    assert (row.weight, row.skip_rides) == (0.5, 2)
    assert (_row(db_session, b).weight, _row(db_session, b).skip_rides) == (0.0, 4)
    assert not _row(db_session, b).reason.startswith("dropped: you skipped")  # the ladder never writes the legacy text
    _ride(db_session, 2, [(a, "skip", 2.0, 10)])
    assert _row(db_session, a).weight == 0.25  # the next counted skip is the 25% step


def test_summary_lists_every_skipped_track_and_replay_keeps_it(db_session):
    fast, slow, dj, played = _tracks(db_session, 4)
    out = _ride(db_session, 0, [(fast, "skip", 2.0, 10), (slow, "skip", 6.0, 300),
                                (dj, "skip", 1.0, 600), (played, "complete", 200, 900)], advances=[598])
    assert out["skipped_track_ids"] == sorted([fast, slow, dj])
    again = dj_learn.learn(db_session, _uid(db_session), T0, T0 + timedelta(hours=1), "r0", advance_times=[])
    assert again["already_learned"] is True and again["skipped_track_ids"] == sorted([fast, slow, dj])


def test_whole_album_ids_read_from_the_pool_state(tmp_path, monkeypatch):
    state = tmp_path / "pool.json"
    state.write_text(json.dumps({"lanes": {"bucket: whole albums": {"track_ids": [3, 4]},
                                           "faster": {"track_ids": [5]}}}), encoding="utf-8")
    monkeypatch.setenv("AUDIPLEX_DJ_POOL_STATE", str(state))
    assert dj_learn.whole_album_track_ids() == {3, 4}
    state.write_text("not json", encoding="utf-8")
    assert dj_learn.whole_album_track_ids() == set()


def test_a_ladder_drop_is_never_rewritten_by_the_migration(db_session):
    (a,) = _tracks(db_session, 1)
    for n in range(5):  # dropped on ride 4, skipped again on ride 5 (played by hand)
        _ride(db_session, n, [(a, "skip", 1.0, 10)])
    assert (_row(db_session, a).weight, _row(db_session, a).skip_rides) == (0.0, 5)
    _ride(db_session, 5, [])
    assert _row(db_session, a).skip_rides == 5


def test_love_takes_a_laddered_track_off_the_ladder(db_session):
    from audiplex.models import DjTrackTag

    (a,) = _tracks(db_session, 1)
    _ride(db_session, 0, [(a, "skip", 1.0, 10)])
    _ride(db_session, 1, [(a, "skip", 1.0, 10)])
    assert _row(db_session, a).weight == 0.5
    db_session.add(DjTrackTag(track_id=a, tag="loved", created_at=T0 + timedelta(days=2, minutes=1)))
    db_session.commit()
    _ride(db_session, 2, [])
    row = _row(db_session, a)
    assert (row.weight, row.skip_rides) == (1.3, 0)


def test_restore_callers_gate_on_four_stars(client, db_session):
    """restore() itself is ungated; every caller gates on 4+ stars or the loved tag (#6199 review)."""
    import inspect

    from audiplex import rating_floor
    from audiplex.routers import music, playback

    for mod in (music, playback, rating_floor):
        src = inspect.getsource(mod)
        for i, line in enumerate(src.splitlines()):
            if "restore(db" in line and "def " not in line:
                window = " ".join(src.splitlines()[max(0, i - 6):i + 1])
                assert "LOVE_MIN_STARS" in window or '"loved"' in window, (mod.__name__, line)
