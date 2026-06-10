"""Tests for the music API (`/api/music/*`)."""

from sqlalchemy.orm import sessionmaker

from audiplex.models import (
    Album,
    Artist,
    Favorite,
    Playlist,
    PlaylistTrack,
    PlayStat,
    Track,
    User,
)


def _seed_jazz_classical(db_engine):
    """Build a small fixture: Miles Davis (Jazz, two albums), Beethoven (Classical, one)."""
    Session = sessionmaker(bind=db_engine)
    session = Session()
    miles = Artist(name="Miles Davis")
    beethoven = Artist(name="Beethoven")
    session.add_all([miles, beethoven])
    session.flush()

    kob = Album(
        title="Kind of Blue",
        artist_id=miles.id,
        genre="Jazz",
        year=1959,
        duration_seconds=2700.0,
        track_count=5,
        folder_path="/m/Miles/Kind of Blue",
    )
    bb = Album(
        title="Bitches Brew",
        artist_id=miles.id,
        genre="Jazz",
        year=1970,
        duration_seconds=6000.0,
        track_count=6,
        folder_path="/m/Miles/Bitches Brew",
    )
    nine = Album(
        title="Symphony No. 9",
        artist_id=beethoven.id,
        genre="Classical",
        year=1824,
        duration_seconds=4200.0,
        track_count=4,
        folder_path="/m/Beethoven/Ninth",
    )
    session.add_all([kob, bb, nine])
    session.commit()
    ids = {
        "miles": miles.id,
        "beethoven": beethoven.id,
        "kob": kob.id,
        "bb": bb.id,
        "nine": nine.id,
    }
    session.close()
    return ids


def _add_album_with_tracks(db_engine, artist_name, album_title, genre, tracks):
    """tracks: list of (disc, num, title)."""
    Session = sessionmaker(bind=db_engine)
    session = Session()
    artist = session.query(Artist).filter(Artist.name == artist_name).first()
    if not artist:
        artist = Artist(name=artist_name)
        session.add(artist)
        session.flush()
    album = Album(
        title=album_title,
        artist_id=artist.id,
        genre=genre,
        folder_path=f"/m/{artist_name}/{album_title}",
    )
    session.add(album)
    session.flush()
    track_ids = []
    for disc, num, title in tracks:
        t = Track(
            title=title,
            album_id=album.id,
            artist_id=artist.id,
            disc_number=disc,
            track_number=num,
            duration_seconds=180.0,
            file_path=f"/m/{artist_name}/{album_title}/d{disc}t{num}.mp3",
        )
        session.add(t)
        session.flush()
        track_ids.append(t.id)
    session.commit()
    album_id = album.id
    artist_id = artist.id
    session.close()
    return album_id, artist_id, track_ids


class TestGenres:
    def test_genres_list(self, client, db_engine):
        _seed_jazz_classical(db_engine)
        resp = client.get("/api/music/genres")
        assert resp.status_code == 200
        data = resp.json()
        names = {g["name"]: g["album_count"] for g in data}
        assert names == {"Jazz": 2, "Classical": 1}

    def test_genres_skips_null(self, client, db_engine):
        _seed_jazz_classical(db_engine)
        # Add an album with no genre
        Session = sessionmaker(bind=db_engine)
        session = Session()
        artist = Artist(name="Mystery Loner")
        session.add(artist)
        session.flush()
        session.add(Album(
            title="Untagged",
            artist_id=artist.id,
            genre=None,
            folder_path="/m/mystery/untagged",
        ))
        session.commit()
        session.close()

        resp = client.get("/api/music/genres")
        names = {g["name"] for g in resp.json()}
        assert None not in names
        assert names == {"Jazz", "Classical"}


class TestArtists:
    def test_artists_list_ordered(self, client, db_engine):
        _seed_jazz_classical(db_engine)
        resp = client.get("/api/music/artists")
        assert resp.status_code == 200
        names = [a["name"] for a in resp.json()]
        assert names == ["Beethoven", "Miles Davis"]

    def test_artist_detail_includes_albums(self, client, db_engine):
        ids = _seed_jazz_classical(db_engine)
        resp = client.get(f"/api/music/artists/{ids['miles']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Miles Davis"
        titles = [a["title"] for a in data["albums"]]
        assert titles == ["Bitches Brew", "Kind of Blue"]
        # artist_name hydrated on each album
        assert all(a["artist_name"] == "Miles Davis" for a in data["albums"])

    def test_artist_detail_404(self, client):
        resp = client.get("/api/music/artists/9999")
        assert resp.status_code == 404

    def test_artist_tracks_flat_list_ordered(self, client, db_engine):
        # Two albums for the same artist, second album seeded first.
        _, artist_id, _ = _add_album_with_tracks(
            db_engine,
            "Prolific Artist",
            "Zebra Album",
            "Rock",
            [(1, 2, "Zebra T2"), (1, 1, "Zebra T1")],
        )
        _add_album_with_tracks(
            db_engine,
            "Prolific Artist",
            "Aardvark Album",
            "Rock",
            [(2, 1, "Aardvark D2T1"), (1, 1, "Aardvark D1T1")],
        )
        resp = client.get(f"/api/music/artists/{artist_id}/tracks")
        assert resp.status_code == 200
        titles = [t["title"] for t in resp.json()]
        # Aardvark album sorts before Zebra; within each, (disc, track) ascending.
        assert titles == [
            "Aardvark D1T1",
            "Aardvark D2T1",
            "Zebra T1",
            "Zebra T2",
        ]

    def test_artist_tracks_404(self, client):
        resp = client.get("/api/music/artists/9999/tracks")
        assert resp.status_code == 404


class TestAlbums:
    def test_albums_unfiltered(self, client, db_engine):
        _seed_jazz_classical(db_engine)
        resp = client.get("/api/music/albums")
        assert resp.status_code == 200
        titles = sorted(a["title"] for a in resp.json())
        assert titles == ["Bitches Brew", "Kind of Blue", "Symphony No. 9"]

    def test_albums_filter_by_genre(self, client, db_engine):
        _seed_jazz_classical(db_engine)
        resp = client.get("/api/music/albums?genre=Jazz")
        titles = sorted(a["title"] for a in resp.json())
        assert titles == ["Bitches Brew", "Kind of Blue"]

    def test_albums_filter_by_artist_id(self, client, db_engine):
        ids = _seed_jazz_classical(db_engine)
        resp = client.get(f"/api/music/albums?artist_id={ids['miles']}")
        titles = sorted(a["title"] for a in resp.json())
        assert titles == ["Bitches Brew", "Kind of Blue"]

    def test_albums_filter_combined(self, client, db_engine):
        ids = _seed_jazz_classical(db_engine)
        resp = client.get(f"/api/music/albums?genre=Jazz&artist_id={ids['miles']}")
        titles = sorted(a["title"] for a in resp.json())
        assert titles == ["Bitches Brew", "Kind of Blue"]
        # Cross-genre filter returns nothing
        resp = client.get(f"/api/music/albums?genre=Classical&artist_id={ids['miles']}")
        assert resp.json() == []

    def test_album_detail_includes_tracks_ordered(self, client, db_engine):
        album_id, _, _ = _add_album_with_tracks(
            db_engine,
            "Multi Disc Artist",
            "Multi Disc Album",
            "Rock",
            [
                (2, 1, "D2 T1"),
                (1, 2, "D1 T2"),
                (1, 1, "D1 T1"),
                (2, 2, "D2 T2"),
            ],
        )
        resp = client.get(f"/api/music/albums/{album_id}")
        assert resp.status_code == 200
        data = resp.json()
        ordered = [(t["disc_number"], t["track_number"]) for t in data["tracks"]]
        assert ordered == [(1, 1), (1, 2), (2, 1), (2, 2)]
        assert data["artist_name"] == "Multi Disc Artist"

    def test_album_detail_404(self, client):
        resp = client.get("/api/music/albums/99999")
        assert resp.status_code == 404


class TestTracks:
    def test_track_detail(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "Solo Artist", "Solo Album", "Indie", [(1, 1, "Solo Track")],
        )
        resp = client.get(f"/api/music/tracks/{track_ids[0]}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Solo Track"
        assert data["artist_name"] == "Solo Artist"

    def test_track_detail_404(self, client):
        resp = client.get("/api/music/tracks/99999")
        assert resp.status_code == 404


def _insert_track_with_real_file(db_engine, file_path, file_size):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    artist = Artist(name="Stream Artist")
    session.add(artist)
    session.flush()
    album = Album(
        title="Stream Album",
        artist_id=artist.id,
        folder_path="/stream/album",
    )
    session.add(album)
    session.flush()
    track = Track(
        title="Stream Track",
        album_id=album.id,
        artist_id=artist.id,
        disc_number=1,
        track_number=1,
        duration_seconds=60.0,
        file_path=file_path,
        file_size=file_size,
    )
    session.add(track)
    session.commit()
    track_id = track.id
    album_id = album.id
    session.close()
    return track_id, album_id


class TestStreamTrack:
    def test_stream_track_full_file(self, client, db_engine, tmp_path):
        content = b"FAKEMP3DATA_" * 1000
        path = tmp_path / "music.mp3"
        path.write_bytes(content)
        track_id, _ = _insert_track_with_real_file(db_engine, str(path), len(content))

        resp = client.get(f"/api/music/stream/track/{track_id}")
        assert resp.status_code == 200
        assert resp.headers["accept-ranges"] == "bytes"
        assert int(resp.headers["content-length"]) == len(content)
        assert resp.headers["content-type"].startswith("audio/mpeg")
        assert len(resp.content) == len(content)

    def test_stream_track_range(self, client, db_engine, tmp_path):
        content = b"X" * 8192
        path = tmp_path / "music.mp3"
        path.write_bytes(content)
        track_id, _ = _insert_track_with_real_file(db_engine, str(path), len(content))

        resp = client.get(
            f"/api/music/stream/track/{track_id}",
            headers={"Range": "bytes=0-1023"},
        )
        assert resp.status_code == 206
        assert len(resp.content) == 1024
        assert resp.headers["content-range"] == f"bytes 0-1023/{len(content)}"

    def test_stream_track_404_missing_file(self, client, db_engine):
        track_id, _ = _insert_track_with_real_file(db_engine, "/no/such/file.mp3", 100)
        resp = client.get(f"/api/music/stream/track/{track_id}")
        assert resp.status_code == 404

    def test_stream_track_404_missing_row(self, client):
        resp = client.get("/api/music/stream/track/99999")
        assert resp.status_code == 404


class TestAlbumCover:
    def test_album_cover_200(self, client, db_engine, tmp_path, monkeypatch):
        from audiplex.config import Settings
        from audiplex.routers import music as music_router

        cover_dir = tmp_path / "covers"
        cover_dir.mkdir()

        _, album_id = _insert_track_with_real_file(db_engine, "/no.mp3", 0)

        cover_bytes = b"\xff\xd8\xff" + b"FAKEJPG"
        (cover_dir / f"album_{album_id}.jpg").write_bytes(cover_bytes)

        monkeypatch.setattr(
            music_router,
            "get_settings",
            lambda: Settings(cover_cache_dir=str(cover_dir)),
        )

        resp = client.get(f"/api/music/covers/album/{album_id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == cover_bytes

    def test_album_cover_404_no_file(self, client, db_engine, tmp_path, monkeypatch):
        from audiplex.config import Settings
        from audiplex.routers import music as music_router

        cover_dir = tmp_path / "covers"
        cover_dir.mkdir()

        _, album_id = _insert_track_with_real_file(db_engine, "/no.mp3", 0)

        monkeypatch.setattr(
            music_router,
            "get_settings",
            lambda: Settings(cover_cache_dir=str(cover_dir)),
        )

        resp = client.get(f"/api/music/covers/album/{album_id}")
        assert resp.status_code == 404

    def test_album_cover_404_no_album(self, client):
        resp = client.get("/api/music/covers/album/99999")
        assert resp.status_code == 404


class TestStats:
    def test_post_stat_creates_row(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "Stat Artist", "Stat Album", "Pop", [(1, 1, "Stat Track")],
        )
        body = {"track_id": track_ids[0], "event": "completed", "played_seconds": 175.5}
        resp = client.post("/api/music/stats", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["track_id"] == track_ids[0]
        assert data["event"] == "completed"
        assert data["played_seconds"] == 175.5
        assert "timestamp" in data

        # verify persisted
        Session = sessionmaker(bind=db_engine)
        session = Session()
        rows = session.query(PlayStat).all()
        assert len(rows) == 1
        assert rows[0].event == "completed"
        session.close()


class TestPlaylists:
    def test_playlists_list_includes_track_count(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "Pl Artist", "Pl Album", "Rock",
            [(1, 1, "T1"), (1, 2, "T2"), (1, 3, "T3")],
        )
        Session = sessionmaker(bind=db_engine)
        session = Session()
        uid = session.query(User).first().id
        p = Playlist(name="Mix", user_id=uid)
        session.add(p)
        session.flush()
        session.add(PlaylistTrack(playlist_id=p.id, track_id=track_ids[0], position=0))
        session.add(PlaylistTrack(playlist_id=p.id, track_id=track_ids[1], position=1))
        session.commit()
        session.close()

        resp = client.get("/api/music/playlists")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Mix"
        assert data[0]["track_count"] == 2

    def test_playlist_detail_tracks_ordered_by_position(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "Order Artist", "Order Album", "Rock",
            [(1, 1, "A"), (1, 2, "B"), (1, 3, "C")],
        )
        Session = sessionmaker(bind=db_engine)
        session = Session()
        uid = session.query(User).first().id
        p = Playlist(name="Ordered", user_id=uid)
        session.add(p)
        session.flush()
        # Insert out of order
        session.add(PlaylistTrack(playlist_id=p.id, track_id=track_ids[2], position=0))
        session.add(PlaylistTrack(playlist_id=p.id, track_id=track_ids[0], position=1))
        session.add(PlaylistTrack(playlist_id=p.id, track_id=track_ids[1], position=2))
        session.commit()
        playlist_id = p.id
        session.close()

        resp = client.get(f"/api/music/playlists/{playlist_id}")
        assert resp.status_code == 200
        data = resp.json()
        titles = [t["title"] for t in data["tracks"]]
        assert titles == ["C", "A", "B"]
        assert data["track_count"] == 3

    def test_create_playlist_with_tracks(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "Cr Artist", "Cr Album", "Rock",
            [(1, 1, "T1"), (1, 2, "T2")],
        )
        body = {"name": "New", "track_ids": track_ids}
        resp = client.post("/api/music/playlists", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "New"
        assert data["track_count"] == 2
        assert [t["id"] for t in data["tracks"]] == track_ids

    def test_update_playlist_rename_only(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "U Artist", "U Album", "Rock", [(1, 1, "T")],
        )
        Session = sessionmaker(bind=db_engine)
        session = Session()
        uid = session.query(User).first().id
        p = Playlist(name="Old", user_id=uid)
        session.add(p)
        session.flush()
        session.add(PlaylistTrack(playlist_id=p.id, track_id=track_ids[0], position=0))
        session.commit()
        playlist_id = p.id
        session.close()

        resp = client.put(
            f"/api/music/playlists/{playlist_id}", json={"name": "Renamed"}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"
        # Track list untouched
        assert resp.json()["track_count"] == 1

    def test_update_playlist_replaces_tracks(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "R Artist", "R Album", "Rock",
            [(1, 1, "A"), (1, 2, "B"), (1, 3, "C"), (1, 4, "D")],
        )
        Session = sessionmaker(bind=db_engine)
        session = Session()
        uid = session.query(User).first().id
        p = Playlist(name="Repl", user_id=uid)
        session.add(p)
        session.flush()
        session.add(PlaylistTrack(playlist_id=p.id, track_id=track_ids[0], position=0))
        session.add(PlaylistTrack(playlist_id=p.id, track_id=track_ids[1], position=1))
        session.commit()
        playlist_id = p.id
        session.close()

        new_order = [track_ids[3], track_ids[2], track_ids[0]]
        resp = client.put(
            f"/api/music/playlists/{playlist_id}",
            json={"track_ids": new_order},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert [t["id"] for t in data["tracks"]] == new_order

        # Positions are 0..n-1, dense
        Session = sessionmaker(bind=db_engine)
        session = Session()
        rows = (
            session.query(PlaylistTrack)
            .filter(PlaylistTrack.playlist_id == playlist_id)
            .order_by(PlaylistTrack.position)
            .all()
        )
        assert [r.position for r in rows] == [0, 1, 2]
        assert [r.track_id for r in rows] == new_order
        session.close()

    def test_update_playlist_invalid_track_id_400(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "I Artist", "I Album", "Rock", [(1, 1, "T")],
        )
        Session = sessionmaker(bind=db_engine)
        session = Session()
        uid = session.query(User).first().id
        p = Playlist(name="Bad", user_id=uid)
        session.add(p)
        session.commit()
        playlist_id = p.id
        session.close()

        resp = client.put(
            f"/api/music/playlists/{playlist_id}",
            json={"track_ids": [track_ids[0], 99999]},
        )
        assert resp.status_code == 400
        assert "99999" in resp.json()["detail"]

    def test_append_to_playlist(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "Ap Artist", "Ap Album", "Rock",
            [(1, 1, "A"), (1, 2, "B"), (1, 3, "C"), (1, 4, "D")],
        )
        Session = sessionmaker(bind=db_engine)
        session = Session()
        uid = session.query(User).first().id
        p = Playlist(name="Growing", user_id=uid)
        session.add(p)
        session.flush()
        session.add(PlaylistTrack(playlist_id=p.id, track_id=track_ids[0], position=0))
        session.commit()
        playlist_id = p.id
        session.close()

        resp = client.post(
            f"/api/music/playlists/{playlist_id}/tracks",
            json={"track_ids": [track_ids[2], track_ids[3]]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert [t["id"] for t in data["tracks"]] == [track_ids[0], track_ids[2], track_ids[3]]
        assert data["track_count"] == 3

    def test_append_to_playlist_404(self, client):
        resp = client.post(
            "/api/music/playlists/9999/tracks", json={"track_ids": []}
        )
        assert resp.status_code == 404

    def test_delete_playlist_cascades_entries(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "D Artist", "D Album", "Rock", [(1, 1, "T")],
        )
        Session = sessionmaker(bind=db_engine)
        session = Session()
        uid = session.query(User).first().id
        p = Playlist(name="ToDel", user_id=uid)
        session.add(p)
        session.flush()
        session.add(PlaylistTrack(playlist_id=p.id, track_id=track_ids[0], position=0))
        session.commit()
        playlist_id = p.id
        session.close()

        resp = client.delete(f"/api/music/playlists/{playlist_id}")
        assert resp.status_code == 200

        Session = sessionmaker(bind=db_engine)
        session = Session()
        assert session.query(Playlist).filter(Playlist.id == playlist_id).first() is None
        assert (
            session.query(PlaylistTrack)
            .filter(PlaylistTrack.playlist_id == playlist_id)
            .count()
            == 0
        )
        session.close()


class TestGenreTracks:
    def test_genre_tracks_ordered(self, client, db_engine):
        _add_album_with_tracks(
            db_engine, "Z Artist", "Zebra", "Jazz", [(1, 1, "Z1"), (1, 2, "Z2")],
        )
        _add_album_with_tracks(
            db_engine, "A Artist", "Apple", "Jazz", [(1, 2, "A2"), (1, 1, "A1")],
        )
        _add_album_with_tracks(
            db_engine, "Mix Artist", "Mix Album", "Rock", [(1, 1, "Should Not Appear")],
        )
        resp = client.get("/api/music/genres/Jazz/tracks")
        assert resp.status_code == 200
        titles = [t["title"] for t in resp.json()]
        # A Artist sorts before Z Artist; within each artist/album, by track #.
        assert titles == ["A1", "A2", "Z1", "Z2"]

    def test_genre_tracks_empty(self, client):
        resp = client.get("/api/music/genres/NoSuchGenre/tracks")
        assert resp.status_code == 200
        assert resp.json() == []


class TestFavorites:
    def test_add_and_list(self, client):
        resp = client.post(
            "/api/music/favorites",
            json={"entity_type": "track", "entity_key": "42"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["entity_type"] == "track"
        assert data["entity_key"] == "42"

        resp = client.get("/api/music/favorites")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_add_is_idempotent(self, client):
        body = {"entity_type": "album", "entity_key": "7"}
        first = client.post("/api/music/favorites", json=body).json()
        second = client.post("/api/music/favorites", json=body).json()
        assert first["id"] == second["id"]

        resp = client.get("/api/music/favorites?entity_type=album")
        assert len(resp.json()) == 1

    def test_filter_by_entity_type(self, client):
        client.post(
            "/api/music/favorites",
            json={"entity_type": "track", "entity_key": "1"},
        )
        client.post(
            "/api/music/favorites",
            json={"entity_type": "album", "entity_key": "2"},
        )
        client.post(
            "/api/music/favorites",
            json={"entity_type": "genre", "entity_key": "Jazz"},
        )
        track_only = client.get("/api/music/favorites?entity_type=track").json()
        assert len(track_only) == 1
        assert track_only[0]["entity_key"] == "1"

    def test_delete(self, client):
        client.post(
            "/api/music/favorites",
            json={"entity_type": "artist", "entity_key": "99"},
        )
        resp = client.delete("/api/music/favorites/artist/99")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1
        assert client.get("/api/music/favorites").json() == []

    def test_unknown_entity_type_400(self, client):
        resp = client.post(
            "/api/music/favorites",
            json={"entity_type": "spaceship", "entity_key": "1"},
        )
        assert resp.status_code == 400


def _add_play_stat(db_engine, track_id, event, played_seconds=0.0):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    user = session.query(User).first()
    session.add(
        PlayStat(track_id=track_id, user_id=user.id, event=event, played_seconds=played_seconds)
    )
    session.commit()
    session.close()


class TestMostPlayed:
    def test_orders_by_play_count(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "MP Artist", "MP Album", "Pop",
            [(1, 1, "Track A"), (1, 2, "Track B"), (1, 3, "Track C")],
        )
        # A: 3 completes
        for _ in range(3):
            _add_play_stat(db_engine, track_ids[0], "complete", 200.0)
        # B: 1 complete + 1 long stop (counts) + 1 early-skip stop (doesn't)
        _add_play_stat(db_engine, track_ids[1], "complete", 200.0)
        _add_play_stat(db_engine, track_ids[1], "stop", 120.0)
        _add_play_stat(db_engine, track_ids[1], "stop", 2.0)
        # C: only starts (not counted)
        _add_play_stat(db_engine, track_ids[2], "start", 0.0)

        resp = client.get("/api/music/most-played?limit=10")
        assert resp.status_code == 200
        ordered = [(t["title"], t["id"]) for t in resp.json()]
        assert [t[0] for t in ordered] == ["Track A", "Track B"]

    def test_default_limit(self, client):
        resp = client.get("/api/music/most-played")
        assert resp.status_code == 200
        assert resp.json() == []


class TestLikelySkips:
    def test_finds_repeat_offenders(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "Sk Artist", "Sk Album", "Pop",
            [(1, 1, "Skippy"), (1, 2, "Liked"), (1, 3, "Once Skipped")],
        )
        # Skippy: 3 early skips (over min_skips=2)
        for _ in range(3):
            _add_play_stat(db_engine, track_ids[0], "skip", 4.0)
        _add_play_stat(db_engine, track_ids[0], "start", 0.0)
        _add_play_stat(db_engine, track_ids[0], "start", 0.0)
        _add_play_stat(db_engine, track_ids[0], "start", 0.0)
        # Liked: never skipped early — just completes
        _add_play_stat(db_engine, track_ids[1], "complete", 180.0)
        # Once Skipped: only one early skip (below default min)
        _add_play_stat(db_engine, track_ids[2], "skip", 3.0)
        _add_play_stat(db_engine, track_ids[2], "start", 0.0)

        resp = client.get("/api/music/likely-skips")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["track"]["title"] == "Skippy"
        assert data[0]["early_skip_count"] == 3
        assert data[0]["total_starts"] == 3

    def test_long_stop_does_not_count(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "L Artist", "L Album", "Pop", [(1, 1, "Long Listen")],
        )
        # Two stops, both well past the early-skip window.
        _add_play_stat(db_engine, track_ids[0], "stop", 120.0)
        _add_play_stat(db_engine, track_ids[0], "stop", 200.0)
        resp = client.get("/api/music/likely-skips?min_skips=1")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_min_skips_threshold(self, client, db_engine):
        _, _, track_ids = _add_album_with_tracks(
            db_engine, "T Artist", "T Album", "Pop", [(1, 1, "Edge")],
        )
        # Two short stops — enough at min_skips=2, not at default.
        _add_play_stat(db_engine, track_ids[0], "stop", 5.0)
        _add_play_stat(db_engine, track_ids[0], "stop", 6.0)
        resp = client.get("/api/music/likely-skips?min_skips=2")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
