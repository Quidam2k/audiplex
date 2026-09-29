"""#ride0928: play history, DJ pair notes, content kinds, owner playlists, ratings."""

from datetime import datetime, timedelta, timezone

from audiplex.models import DjPairNote, Playlist, PlayStat, Track, TrackRating, User


def _owner(db):
    return db.query(User).filter(User.username == "testuser").first()


def _track(db, album, title, path):
    t = Track(title=title, album_id=album.id, artist_id=album.artist_id, file_path=path, file_size=1)
    db.add(t)
    db.commit()
    return t


def _owner_is_testuser(monkeypatch):
    from audiplex.routers import playback

    s = playback.get_settings().model_copy(update={"dj_owner_username": "testuser"})
    monkeypatch.setattr(playback, "get_settings", lambda: s)


def test_history_is_owner_scoped_newest_first(client, db_session, sample_album, monkeypatch):
    _owner_is_testuser(monkeypatch)
    owner = _owner(db_session)
    a = _track(db_session, sample_album, "A", "/x/a.mp3")
    b = _track(db_session, sample_album, "B", "/x/b.mp3")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add_all([
        PlayStat(track_id=a.id, user_id=owner.id, event="start", timestamp=now - timedelta(minutes=10)),
        PlayStat(track_id=a.id, user_id=owner.id, event="complete", timestamp=now - timedelta(minutes=6)),
        PlayStat(track_id=b.id, user_id=owner.id, event="start", timestamp=now - timedelta(minutes=5)),
        PlayStat(track_id=b.id, user_id=None, event="start", timestamp=now),  # not the owner
    ])
    db_session.commit()
    rows = client.get("/api/playback/history?limit=5").json()
    assert [r["title"] for r in rows] == ["B", "A"]
    assert rows[0]["event"] == "start" and rows[0]["at"] > rows[1]["at"]
    assert len(client.get("/api/playback/history?events=all").json()) == 3
    since = (datetime.now(timezone.utc) - timedelta(minutes=7)).timestamp()
    assert [r["title"] for r in client.get(f"/api/playback/history?since={since}").json()] == ["B"]


def test_pair_notes_roundtrip(client, db_session, sample_album):
    a = _track(db_session, sample_album, "A", "/x/a.mp3")
    b = _track(db_session, sample_album, "B", "/x/b.mp3")
    c = _track(db_session, sample_album, "C", "/x/c.mp3")
    post = lambda **kw: client.post("/api/playback/pair-notes", json=kw)
    assert post(track_a=a.id, track_b=b.id, note="great lift", persona="claude").status_code == 200
    assert post(track_a=b.id, note="loud intro").status_code == 200
    assert post(track_a=a.id, track_b=c.id, note="clash").status_code == 200
    assert post(track_a=a.id, note="").status_code == 400
    assert post(track_a=999999, note="x").status_code == 404
    pair = client.get(f"/api/playback/pair-notes?track_a={a.id}&track_b={b.id}").json()
    assert sorted(n["note"] for n in pair) == ["great lift", "loud intro"]
    touching = client.get(f"/api/playback/pair-notes?track_a={a.id}").json()
    assert {n["note"] for n in touching} == {"great lift", "clash"}
    assert db_session.query(DjPairNote).count() == 3


def test_content_kind_by_ids_and_folder_and_mix_excludes(client, db_session, sample_album, tmp_path, monkeypatch):
    _owner_is_testuser(monkeypatch)
    pod = tmp_path / "Podcasts"
    pod.mkdir()
    song = _track(db_session, sample_album, "Song", str(tmp_path / "song.mp3"))
    ep = _track(db_session, sample_album, "Ep 1", str(pod / "ep1.mp3"))
    ep2 = _track(db_session, sample_album, "Ep 2", str(pod / "ep2.mp3"))
    assert song.content_kind == "music"
    r = client.put("/api/playback/content-kind", json={"kind": "podcast", "folder": str(pod)})
    assert r.json() == {"kind": "podcast", "updated": 2}
    assert client.put("/api/playback/content-kind", json={"kind": "nope", "track_ids": [song.id]}).status_code == 400
    plan = client.post("/api/playback/mix/plan", json={"new_ids": [song.id, ep.id, ep2.id], "shuffle": False}).json()
    assert plan["upcoming"] == [song.id]
    r = client.put("/api/playback/content-kind", json={"kind": "music", "track_ids": [ep.id]})
    assert r.json()["updated"] == 1


def test_owner_playlist_create(client, db_session, sample_album, monkeypatch):
    _owner_is_testuser(monkeypatch)
    a = _track(db_session, sample_album, "A", "/x/a.mp3")
    r = client.post("/api/playback/playlists", json={"name": "Ride folder", "track_ids": [a.id, 999999]})
    assert r.status_code == 200 and r.json()["track_count"] == 1
    pl = db_session.query(Playlist).filter(Playlist.name == "Ride folder").one()
    assert pl.user_id == _owner(db_session).id


def test_phone_rating_reaches_dj_ratings_read(client, db_session, sample_track, monkeypatch):
    """#3024 end to end: the phone's PUT lands where dj_track_ratings reads."""
    _owner_is_testuser(monkeypatch)
    r = client.put(f"/api/music/tracks/{sample_track.id}/rating", json={"rating": 5, "note": "ride banger"})
    assert r.status_code in (200, 201), r.text
    rows = client.get("/api/playback/ratings").json()
    assert rows and rows[0]["track_id"] == sample_track.id and rows[0]["rating"] == 5
    assert db_session.query(TrackRating).count() == 1


def test_scanner_content_kind_rules(db_session, sample_album, tmp_path):
    from audiplex.scanners.music import apply_content_kinds, kind_for_path

    assert kind_for_path("Podcasts/Show/ep1.mp3") == "podcast"
    assert kind_for_path("Ambient/Eno/1-1.mp3") == "music"  # a genre, not a kind
    assert kind_for_path("anything/x.mp3", "ambient") == "ambient"
    root = tmp_path / "lib"
    song = _track(db_session, sample_album, "S", str(root / "Rock" / "s.mp3"))
    ep = _track(db_session, sample_album, "E", str(root / "podcasts" / "e.mp3"))
    fx = _track(db_session, sample_album, "F", str(root / "SFX" / "f.mp3"))
    other = _track(db_session, sample_album, "O", str(tmp_path / "elsewhere" / "podcasts" / "o.mp3"))
    assert apply_content_kinds(db_session, str(root)) == 2
    assert (song.content_kind, ep.content_kind, fx.content_kind, other.content_kind) == \
        ("music", "podcast", "clip", "music")
    assert apply_content_kinds(db_session, str(root), "ambient") == 1  # only the one still 'music'
    assert song.content_kind == "ambient" and ep.content_kind == "podcast"
