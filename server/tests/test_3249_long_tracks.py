"""#3249: a music mix or pool never picks up an album-length file.

2026-09-28 21:49: "Mr. Robot OST Vol 4" (62 min, indexed with #5472's folders)
landed in the ride mix and Todd skipped it at 9 minutes. Mixes and pools now drop
music over 20 minutes, but only from multi-track sources: a one-track source was
asked for by name and is kept. Non-music is the #ride0928 filter's job.
"""

from audiplex.models import Track
from audiplex.routers import playback


def _track(db, album, title, minutes, kind="music"):
    t = Track(title=title, album_id=album.id, artist_id=album.artist_id,
              file_path=f"/m/{title}.mp3", file_size=1,
              duration_seconds=minutes * 60, content_kind=kind)
    db.add(t)
    db.commit()
    return t


def _owner_is_testuser(monkeypatch):
    s = playback.get_settings().model_copy(update={"dj_owner_username": "testuser"})
    monkeypatch.setattr(playback, "get_settings", lambda: s)


def test_mix_plan_drops_long_music(client, db_session, sample_album, monkeypatch):
    _owner_is_testuser(monkeypatch)
    song = _track(db_session, sample_album, "Song", 4)
    edge = _track(db_session, sample_album, "Edge", 20)          # exactly the cap: kept
    ost = _track(db_session, sample_album, "OST Vol 4", 62)
    plan = client.post("/api/playback/mix/plan",
                       json={"new_ids": [song.id, edge.id, ost.id], "shuffle": False}).json()
    assert plan["upcoming"] == [song.id, edge.id]
    assert plan["skipped_long"] == [ost.id]


def test_mix_plan_keeps_a_single_named_long_track(client, db_session, sample_album, monkeypatch):
    _owner_is_testuser(monkeypatch)
    ost = _track(db_session, sample_album, "OST Vol 4", 62)
    plan = client.post("/api/playback/mix/plan",
                       json={"new_ids": [ost.id], "shuffle": False}).json()
    assert plan["upcoming"] == [ost.id]
    assert plan["skipped_long"] == []


def test_long_non_music_is_not_this_filters_business(db_session, sample_album):
    # An audiobook chapter or ambient bed is filtered (or not) by content kind,
    # never reported as a long MUSIC skip.
    a = _track(db_session, sample_album, "Rain", 90, kind="ambient")
    b = _track(db_session, sample_album, "Song", 3)
    assert playback.too_long_for_mix(db_session, [a.id, b.id]) == set()


def test_pool_lanes_drop_long_music_but_keep_one_track_lane(client, db_session, sample_album):
    song = _track(db_session, sample_album, "Song", 4)
    ost = _track(db_session, sample_album, "OST Vol 4", 62)
    solo = _track(db_session, sample_album, "Suite", 40)
    r = client.post("/api/playback/pool", json={
        "lanes": {"mix": [song.id, ost.id], "named": [solo.id]}, "balance": "even",
    })
    assert r.status_code == 200
    assert r.json()["skipped_long"] == [ost.id]
    lanes = {l["label"]: l for l in client.get("/api/playback/pool").json()["lanes"]}
    assert lanes["mix"]["remaining"] == 1
    assert lanes["named"]["remaining"] == 1


def test_pool_legacy_track_ids_drop_long_music(client, db_session, sample_album):
    song = _track(db_session, sample_album, "Song", 4)
    ost = _track(db_session, sample_album, "OST Vol 4", 62)
    r = client.post("/api/playback/pool", json={"track_ids": [song.id, ost.id]})
    assert r.json()["skipped_long"] == [ost.id]
    assert client.get("/api/playback/pool").json()["eligible_count"] == 1


def test_each_skip_is_logged_once(db_session, sample_album, monkeypatch, capsys):
    monkeypatch.setattr(playback, "_long_skip_logged", set())
    ost = _track(db_session, sample_album, "OST Vol 4", 62)
    song = _track(db_session, sample_album, "Song", 4)
    playback.too_long_for_mix(db_session, [ost.id, song.id])
    playback.too_long_for_mix(db_session, [ost.id, song.id])
    assert capsys.readouterr().out.count("OST Vol 4") == 1
