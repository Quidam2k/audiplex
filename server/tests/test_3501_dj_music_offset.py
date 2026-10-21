"""#3501: dormant DJ music offset. Todd's 10/20 ruling (Pantheon msg 46601): if a DJ
ride shows 2+ too-quiet presses, the DJ music drops 3 dB. Default 0 = off; applied to
the served music target only while a DJ pool runs; cut-only."""

from audiplex.config import Settings, get_settings
from audiplex.dj_pool import get_pool
from audiplex.gain import music_target_lufs


def test_offset_is_off_by_default():
    assert Settings.model_fields["dj_music_offset_db"].default == 0.0


def test_target_math_is_cut_only_and_dj_only():
    assert music_target_lufs(-20.0, -3.0, dj_active=True) == -23.0
    assert music_target_lufs(-20.0, -3.0, dj_active=False) == -20.0
    assert music_target_lufs(-20.0, 3.0, dj_active=True) == -20.0  # never louder
    assert music_target_lufs(-20.0, -40.0, dj_active=True) == -32.0  # clamped


def _setup(monkeypatch, db_session, sample_track, offset, active):
    s = get_settings()
    monkeypatch.setattr(s, "music_target_lufs", -20.0)
    monkeypatch.setattr(s, "music_max_boost_db", 0.0)
    monkeypatch.setattr(s, "dj_music_offset_db", offset)
    monkeypatch.setattr(type(get_pool()), "is_active", lambda self: active)
    sample_track.loudness_lufs = -14.0
    db_session.commit()


def test_endpoints_serve_the_offset_while_a_pool_runs(client, db_session, sample_track, monkeypatch):
    _setup(monkeypatch, db_session, sample_track, -3.0, True)
    assert client.get("/api/music/levels").json()["target_lufs"] == -23.0
    assert client.get(f"/api/playback/gain/track/{sample_track.id}").json()["gain_db"] == -9.0
    album = client.get(f"/api/music/albums/{sample_track.album_id}").json()
    assert next(t for t in album["tracks"] if t["id"] == sample_track.id)["gain_db"] == -9.0


def test_no_pool_no_offset(client, db_session, sample_track, monkeypatch):
    _setup(monkeypatch, db_session, sample_track, -3.0, False)
    assert client.get("/api/music/levels").json()["target_lufs"] == -20.0
    assert client.get(f"/api/playback/gain/track/{sample_track.id}").json()["gain_db"] == -6.0


def test_default_off_even_with_a_pool(client, db_session, sample_track, monkeypatch):
    _setup(monkeypatch, db_session, sample_track, 0.0, True)
    assert client.get("/api/music/levels").json()["target_lufs"] == -20.0
