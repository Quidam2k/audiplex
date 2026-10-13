"""#3504 / #4222: GET /api/playback/gain/{kind}/{id} and TrackSchema.gain_db."""

from audiplex.gain import MIN_GAIN_DB, book_gain_db, music_gain_db


def test_music_gain_is_attenuate_only_like_the_phone():
    assert music_gain_db(-14.0, -20.0) == -6.0
    assert music_gain_db(-25.0, -20.0) == 0.0  # quiet track: no boost
    assert music_gain_db(None, -20.0) is None  # #7387 unmeasured: no change
    assert music_gain_db(20.0, -20.0) == round(MIN_GAIN_DB, 2)  # LoudnessGain's 0.05 floor


def test_book_gain_raises_to_the_voice_and_clamps():
    assert book_gain_db(-26.0, -20.0, 12.0) == 6.0
    assert book_gain_db(-40.0, -20.0, 12.0) == 12.0
    assert book_gain_db(-17.0, -20.0, 12.0) == 0.0  # louder than target: never cut
    assert book_gain_db(None, -20.0, 12.0) is None


def test_endpoint_track(client, db_session, sample_track):
    sample_track.loudness_lufs = -14.0
    db_session.commit()
    data = client.get(f"/api/playback/gain/track/{sample_track.id}").json()
    assert data == {"kind": "track", "id": sample_track.id, "lufs": -14.0,
                    "target_lufs": -20.0, "gain_db": -6.0, "mode": "attenuate_only"}


def test_endpoint_book_raise_and_unmeasured(client, db_session, sample_book):
    url = f"/api/playback/gain/book/{sample_book.id}"
    assert client.get(url).json()["gain_db"] is None
    sample_book.loudness_lufs = -27.5
    db_session.commit()
    data = client.get(url).json()
    assert data["gain_db"] == 7.5
    assert data["mode"] == "boost_needs_limiter"


def test_endpoint_errors(client, sample_track):
    assert client.get("/api/playback/gain/chapter/1").status_code == 400
    assert client.get("/api/playback/gain/track/999999").status_code == 404
    assert client.get("/api/playback/gain/book/999999").status_code == 404


def test_track_responses_carry_gain_db(client, db_session, sample_track):
    sample_track.loudness_lufs = -16.0
    db_session.commit()
    data = client.get(f"/api/music/albums/{sample_track.album_id}").json()
    track = next(t for t in data["tracks"] if t["id"] == sample_track.id)
    assert track["gain_db"] == -4.0
