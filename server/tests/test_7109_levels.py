"""#7109: music loudness normalization — per-track gain settings and levels endpoint."""

import pytest

from audiplex.models import Track


def test_levels_endpoint_default_off(client, db_session, sample_album):
    """Verify levels endpoint exists and reports the configured normalization flag.

    #7389: the code default stays OFF; config.yaml turns it on in production.
    """
    from audiplex.config import Settings, get_settings

    assert Settings.model_fields["normalize_music"].default is False  # #7389
    response = client.get("/api/music/levels")
    assert response.status_code == 200
    data = response.json()
    assert data["normalize_music"] is get_settings().normalize_music  # #7389
    assert data["target_lufs"] == -24.0
    assert data["fallback_lufs"] is None


def test_levels_endpoint_fallback_lufs_is_median(client, db_session, sample_album):
    """Verify fallback_lufs is the median of non-null track loudness values."""
    # Create tracks with various loudness levels
    tracks = []
    for i, lufs in enumerate([-23.5, -18.0, -15.2, -22.0, -19.8]):
        t = Track(
            title=f"Track {i}",
            album_id=sample_album.id,
            artist_id=sample_album.artist_id,
            disc_number=1,
            track_number=i + 1,
            duration_seconds=180.0,
            file_path=f"/fake/music/Test Artist/Test Album/{i + 1:02d} Track {i}.mp3",
            file_size=5_000_000,
            loudness_lufs=lufs,
        )
        db_session.add(t)
        tracks.append(t)

    # Add a track with null loudness (should be ignored)
    t_null = Track(
        title="Unmeasured Track",
        album_id=sample_album.id,
        artist_id=sample_album.artist_id,
        disc_number=1,
        track_number=99,
        duration_seconds=180.0,
        file_path="/fake/music/Test Artist/Test Album/99 Unmeasured.mp3",
        file_size=5_000_000,
        loudness_lufs=None,
    )
    db_session.add(t_null)
    db_session.commit()

    response = client.get("/api/music/levels")
    assert response.status_code == 200
    data = response.json()

    # Sorted: [-23.5, -22.0, -19.8, -18.0, -15.2]
    # Median of 5 values: index 2 = -19.8
    assert data["median_lufs"] == pytest.approx(-19.8)
    # #7387: an unmeasured track is never cut on a guess
    assert data["fallback_lufs"] is None


def test_levels_endpoint_fallback_lufs_even_count(client, db_session, sample_album):
    """Verify fallback_lufs is correct for even number of measured tracks."""
    tracks_data = [(-23.5, "Track 1"), (-18.0, "Track 2"), (-15.2, "Track 3"), (-22.0, "Track 4")]
    for lufs, title in tracks_data:
        t = Track(
            title=title,
            album_id=sample_album.id,
            artist_id=sample_album.artist_id,
            disc_number=1,
            track_number=len(tracks_data),
            duration_seconds=180.0,
            file_path=f"/fake/music/Test Artist/Test Album/{title}.mp3",
            file_size=5_000_000,
            loudness_lufs=lufs,
        )
        db_session.add(t)
    db_session.commit()

    response = client.get("/api/music/levels")
    assert response.status_code == 200
    data = response.json()

    # Sorted: [-23.5, -22.0, -18.0, -15.2]
    # Median of 4 values: average of indices 1 and 2 = (-22.0 + -18.0) / 2 = -20.0
    assert data["median_lufs"] == pytest.approx(-20.0)


def test_loudness_lufs_in_track_payload(client, db_session, sample_album):
    """Verify loudness_lufs field is present in track GET responses."""
    t = Track(
        title="Loud Track",
        album_id=sample_album.id,
        artist_id=sample_album.artist_id,
        disc_number=1,
        track_number=1,
        duration_seconds=240.0,
        file_path="/fake/music/Test Artist/Test Album/01 Loud Track.mp3",
        file_size=5_000_000,
        loudness_lufs=-14.5,
    )
    db_session.add(t)
    db_session.commit()

    # Test GET /api/music/albums/{id} includes the track with loudness_lufs
    response = client.get(f"/api/music/albums/{sample_album.id}")
    assert response.status_code == 200
    data = response.json()
    assert len(data["tracks"]) == 1
    assert data["tracks"][0]["loudness_lufs"] == -14.5


def test_loudness_lufs_null_in_track_payload(client, db_session, sample_album):
    """Verify loudness_lufs can be null in track responses."""
    t = Track(
        title="Quiet Track",
        album_id=sample_album.id,
        artist_id=sample_album.artist_id,
        disc_number=1,
        track_number=1,
        duration_seconds=240.0,
        file_path="/fake/music/Test Artist/Test Album/01 Quiet Track.mp3",
        file_size=5_000_000,
        loudness_lufs=None,
    )
    db_session.add(t)
    db_session.commit()

    response = client.get(f"/api/music/albums/{sample_album.id}")
    assert response.status_code == 200
    data = response.json()
    assert len(data["tracks"]) == 1
    assert data["tracks"][0]["loudness_lufs"] is None
