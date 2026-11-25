"""#6999: GET /api/music/tracks?ids= gives fresh rows for the phone's Recently played list."""
from audiplex.models import Track


def _second_track(db_session, sample_track):
    t = Track(title="Second", album_id=sample_track.album_id, artist_id=sample_track.artist_id,
              disc_number=1, track_number=2, duration_seconds=200.0,
              file_path="/fake/music/Test Artist/Test Album/02 Second.mp3", file_size=4_000_000,
              end_trim_seconds=144.0)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


def test_order_kept_unknown_skipped_and_cache_fields_present(client, db_session, sample_track):
    second = _second_track(db_session, sample_track)
    r = client.get(f"/api/music/tracks?ids={second.id},999999,{sample_track.id},{second.id}")
    assert r.status_code == 200
    body = r.json()
    assert [t["id"] for t in body] == [second.id, sample_track.id]  # asked order, no dup, no unknown
    assert body[0]["end_trim_seconds"] == 144.0
    assert body[0]["file_size"] == 4_000_000
    assert body[1]["file_size"] == 5_000_000


def test_a_new_trim_shows_up(client, db_session, sample_track):
    db_session.query(Track).filter(Track.id == sample_track.id).update({Track.end_trim_seconds: 151.299})
    db_session.commit()
    assert client.get(f"/api/music/tracks?ids={sample_track.id}").json()[0]["end_trim_seconds"] == 151.299


def test_cap_and_bad_input(client, sample_track):
    assert client.get("/api/music/tracks?ids=" + ",".join(str(i) for i in range(1, 202))).status_code == 422
    assert client.get("/api/music/tracks?ids=1,x").status_code == 422
    assert client.get("/api/music/tracks?ids=").json() == []


def test_single_track_route_unchanged(client, sample_track):
    assert client.get(f"/api/music/tracks/{sample_track.id}").json()["id"] == sample_track.id
