"""#4118: music is raised toward music_target_lufs by at most music_max_boost_db.

Jarvis default-and-veto option B (2026-10-18): -14 LUFS, +6 dB cap, the app's
limiter carries the raise. Code defaults stay attenuate-only for old apps.
"""

from audiplex.config import Settings, get_settings
from audiplex.gain import music_gain_db


def test_boost_is_capped_and_defaults_to_attenuate_only():
    assert Settings.model_fields["music_max_boost_db"].default == 0.0
    assert music_gain_db(-25.0, -20.0) == 0.0  # default: no raise
    assert music_gain_db(-17.0, -14.0, 6.0) == 3.0
    assert music_gain_db(-30.0, -14.0, 6.0) == 6.0  # capped
    assert music_gain_db(-8.0, -14.0, 6.0) == -6.0  # loud tracks still cut
    assert music_gain_db(-25.0, -20.0, -3.0) == 0.0  # a negative cap never forces a cut
    assert music_gain_db(None, -14.0, 6.0) is None  # #7387 unmeasured: no change


def test_endpoints_report_the_boost(client, db_session, sample_track, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "music_target_lufs", -14.0)
    monkeypatch.setattr(s, "music_max_boost_db", 6.0)
    sample_track.loudness_lufs = -24.0
    db_session.commit()
    gain = client.get(f"/api/playback/gain/track/{sample_track.id}").json()
    assert gain["gain_db"] == 6.0
    assert gain["mode"] == "boost_needs_limiter"
    levels = client.get("/api/music/levels").json()
    assert levels["max_boost_db"] == 6.0
    assert levels["target_lufs"] == -14.0
    album = client.get(f"/api/music/albums/{sample_track.album_id}").json()
    assert next(t for t in album["tracks"] if t["id"] == sample_track.id)["gain_db"] == 6.0


def test_levels_default_max_boost_zero(client):
    assert client.get("/api/music/levels").json()["max_boost_db"] == 0.0
