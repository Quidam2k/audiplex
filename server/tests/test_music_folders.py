"""Tests for the folder-browsing API (`/api/music/folders*`)."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from audiplex.auth import get_current_user
from audiplex.database import get_db
from audiplex.models import Album, Artist, Track, User
from audiplex.routers import music
from audiplex.routers.music import get_music_roots

MUSIC_ROOT = "/m"


def _seed(db_engine):
    """Seed a small music tree under /m.

        /m/Artists & Albums/Jazz/Miles Davis/Kind of Blue   (2 tracks)
        /m/Artists & Albums/Jazz/Miles Davis/Bitches Brew   (1 track)
        /m/Artists & Albums/Classical/Beethoven/Ninth        (1 track)
        /m/Bike Music                                         (1 track, loose)
    """
    Session = sessionmaker(bind=db_engine)
    s = Session()
    # PUT /api/music/roots is admin-gated; the get_current_user override
    # returns the first user, so make that user an admin.
    s.add(User(username="tester", password_hash="x", display_name="Tester", is_admin=True))
    miles = Artist(name="Miles Davis")
    beethoven = Artist(name="Beethoven")
    s.add_all([miles, beethoven])
    s.flush()

    def album(title, artist, genre, folder, tracks):
        al = Album(
            title=title,
            artist_id=artist.id,
            genre=genre,
            folder_path=folder,
            track_count=len(tracks),
            duration_seconds=0.0,
        )
        s.add(al)
        s.flush()
        for disc, num, name in tracks:
            s.add(Track(
                title=name,
                album_id=al.id,
                artist_id=artist.id,
                disc_number=disc,
                track_number=num,
                duration_seconds=0.0,
                file_path=f"{folder}/{num:02d} {name}.mp3",
            ))

    album("Kind of Blue", miles, "Jazz",
          "/m/Artists & Albums/Jazz/Miles Davis/Kind of Blue",
          [(1, 1, "So What"), (1, 2, "Blue in Green")])
    album("Bitches Brew", miles, "Jazz",
          "/m/Artists & Albums/Jazz/Miles Davis/Bitches Brew",
          [(1, 1, "Pharaoh's Dance")])
    album("Ninth", beethoven, "Classical",
          "/m/Artists & Albums/Classical/Beethoven/Ninth",
          [(1, 1, "Movement I")])
    album("Bike Music", miles, None,
          "/m/Bike Music",
          [(1, 1, "Ride")])
    s.commit()
    s.close()


@pytest.fixture
def fclient(db_engine):
    _seed(db_engine)
    app = FastAPI()
    app.include_router(music.router)
    TestSession = sessionmaker(bind=db_engine)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    def override_get_current_user(db: Session = Depends(get_db)):
        return db.query(User).first()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_music_roots] = lambda: [MUSIC_ROOT]

    with TestClient(app) as c:
        yield c


def test_root_lists_music_roots(fclient):
    data = fclient.get("/api/music/folders").json()
    assert data["path"] is None
    assert data["parent"] is None
    assert data["albums"] == []
    assert len(data["folders"]) == 1
    node = data["folders"][0]
    assert node["path"] == "/m"
    assert node["name"] == "m"
    assert node["album_count"] == 4
    assert node["track_count"] == 5  # 2 + 1 + 1 + 1


def test_descend_into_root(fclient):
    data = fclient.get("/api/music/folders", params={"path": "/m"}).json()
    assert sorted(f["name"] for f in data["folders"]) == ["Artists & Albums", "Bike Music"]
    assert data["albums"] == []
    assert data["parent"] is None  # /m is itself a root


def test_descend_to_artist(fclient):
    p = "/m/Artists & Albums/Jazz/Miles Davis"
    data = fclient.get("/api/music/folders", params={"path": p}).json()
    assert sorted(f["name"] for f in data["folders"]) == ["Bitches Brew", "Kind of Blue"]
    assert data["albums"] == []
    assert data["parent"] == "/m/Artists & Albums/Jazz"


def test_album_folder_shows_album(fclient):
    data = fclient.get("/api/music/folders", params={"path": "/m/Bike Music"}).json()
    assert data["folders"] == []
    assert [a["title"] for a in data["albums"]] == ["Bike Music"]
    assert data["parent"] == "/m"


def test_child_node_aggregates_counts(fclient):
    data = fclient.get("/api/music/folders", params={"path": "/m"}).json()
    aa = next(f for f in data["folders"] if f["name"] == "Artists & Albums")
    assert aa["album_count"] == 3
    assert aa["track_count"] == 4


def test_folder_tracks_recursive_and_ordered(fclient):
    p = "/m/Artists & Albums/Jazz/Miles Davis"
    titles = [t["title"] for t in fclient.get(
        "/api/music/folders/tracks", params={"path": p}
    ).json()]
    # Bitches Brew sorts before Kind of Blue by folder path; tracks by number.
    assert titles == ["Pharaoh's Dance", "So What", "Blue in Green"]


def test_folder_tracks_whole_root(fclient):
    titles = [t["title"] for t in fclient.get(
        "/api/music/folders/tracks", params={"path": "/m"}
    ).json()]
    assert len(titles) == 5


def test_backslash_path_matches(fclient):
    # Client may echo a Windows-style path back; it must still resolve.
    p = "\\m\\Artists & Albums\\Jazz\\Miles Davis"
    data = fclient.get("/api/music/folders", params={"path": p}).json()
    assert sorted(f["name"] for f in data["folders"]) == ["Bitches Brew", "Kind of Blue"]


def test_invalid_path_404(fclient):
    r = fclient.get("/api/music/folders", params={"path": "/etc/passwd"})
    assert r.status_code == 404


def test_folder_tracks_unknown_returns_empty(fclient):
    r = fclient.get("/api/music/folders/tracks", params={"path": "/nope"})
    assert r.status_code == 200
    assert r.json() == []


def test_list_roots_reports_existence(fclient, tmp_path):
    # /m is a fake path (won't exist); a real tmp_path dir should report exists.
    real = str(tmp_path)
    fclient.app.dependency_overrides[get_music_roots] = lambda: ["/m", real]
    data = fclient.get("/api/music/roots").json()
    by_path = {r["path"]: r["exists"] for r in data["roots"]}
    assert by_path == {"/m": False, real: True}


def test_update_roots_cleans_and_rescans(fclient, monkeypatch):
    """PUT trims/dedupes/drops-blank paths, persists music roots, and rescans."""
    saved: dict = {}
    monkeypatch.setattr(
        music, "set_library_roots_for_category",
        lambda category, paths, *a, **k: saved.update(category=category, paths=paths),
    )
    scanned = {"count": 0}
    monkeypatch.setattr(
        "audiplex.scanner.scan_library",
        lambda *a, **k: scanned.update(count=scanned["count"] + 1) or {
            "added": 0, "updated": 0, "removed": 0, "errors": []
        },
    )

    body = {"paths": ["  /a  ", "/a", "", "/b"]}
    data = fclient.put("/api/music/roots", json=body).json()

    assert saved == {"category": "music", "paths": ["/a", "/b"]}
    assert scanned["count"] == 1
    assert [r["path"] for r in data["roots"]] == ["/a", "/b"]
