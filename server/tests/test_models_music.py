"""ORM-level tests for the music schema (artists, albums, tracks, playlists, play_stats)."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from audiplex.models import (
    Album,
    Artist,
    Playlist,
    PlaylistTrack,
    PlayStat,
    Track,
)


def _session(db_engine):
    return sessionmaker(bind=db_engine)()


def test_create_artist_album_track_chain(sample_artist, sample_album, sample_track, db_session):
    db_session.refresh(sample_artist)
    db_session.refresh(sample_album)
    db_session.refresh(sample_track)
    assert sample_album in sample_artist.albums
    assert sample_track in sample_album.tracks
    assert sample_track in sample_artist.tracks
    assert sample_track.album is sample_album
    assert sample_track.artist is sample_artist


def test_album_same_artist_title_different_folder_allowed(db_engine, sample_album):
    """Same (artist, title) under different folder_paths is legitimate —
    Todd's library cross-files albums across multiple genre folders."""
    session = _session(db_engine)
    dup = Album(
        title=sample_album.title,
        artist_id=sample_album.artist_id,
        folder_path="/fake/music/different/path",
    )
    session.add(dup)
    session.commit()
    assert dup.id is not None
    assert dup.id != sample_album.id
    session.close()


def test_album_folder_path_unique(db_engine, sample_album):
    session = _session(db_engine)
    dup = Album(
        title="Different Title",
        artist_id=sample_album.artist_id,
        folder_path=sample_album.folder_path,
    )
    session.add(dup)
    with pytest.raises(IntegrityError):
        session.commit()
    session.close()


def test_artist_unique_name(db_engine, sample_artist):
    session = _session(db_engine)
    dup = Artist(name=sample_artist.name)
    session.add(dup)
    with pytest.raises(IntegrityError):
        session.commit()
    session.close()


def test_track_file_path_unique(db_engine, sample_album, sample_track):
    session = _session(db_engine)
    dup = Track(
        title="Different Title",
        album_id=sample_album.id,
        artist_id=sample_album.artist_id,
        file_path=sample_track.file_path,
    )
    session.add(dup)
    with pytest.raises(IntegrityError):
        session.commit()
    session.close()


def test_album_delete_cascades_to_tracks(db_engine, sample_album, sample_track):
    session = _session(db_engine)
    album = session.get(Album, sample_album.id)
    track_id = sample_track.id
    session.delete(album)
    session.commit()
    assert session.get(Track, track_id) is None
    session.close()


def test_playlist_delete_cascades_to_entries(db_engine, sample_playlist):
    session = _session(db_engine)
    playlist = session.get(Playlist, sample_playlist.id)
    entry_id = playlist.entries[0].id
    session.delete(playlist)
    session.commit()
    assert session.get(PlaylistTrack, entry_id) is None
    session.close()


def test_track_delete_cascades_to_playstats_and_playlist_entries(
    db_engine, sample_track, sample_playlist
):
    session = _session(db_engine)
    track = session.get(Track, sample_track.id)
    stat = PlayStat(track_id=track.id, event="completed", played_seconds=180.0)
    session.add(stat)
    session.commit()
    stat_id = stat.id
    entry_id = sample_playlist.entries[0].id

    session.delete(track)
    session.commit()
    assert session.get(PlayStat, stat_id) is None
    assert session.get(PlaylistTrack, entry_id) is None
    session.close()


def test_playlist_track_position_ordering(db_engine, sample_album):
    session = _session(db_engine)
    tracks = []
    for i in range(3):
        t = Track(
            title=f"Track {i}",
            album_id=sample_album.id,
            artist_id=sample_album.artist_id,
            track_number=i,
            file_path=f"/fake/music/ordering/{i}.mp3",
        )
        session.add(t)
        tracks.append(t)
    session.flush()

    playlist = Playlist(name="Order Test")
    session.add(playlist)
    session.flush()

    for pos, track in zip([2, 0, 1], tracks):
        session.add(PlaylistTrack(
            playlist_id=playlist.id,
            track_id=track.id,
            position=pos,
        ))
    session.commit()

    session.refresh(playlist)
    positions = [e.position for e in playlist.entries]
    assert positions == [0, 1, 2]
    session.close()
