"""#3696: whole-library music search."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from audiplex.database import get_db
from audiplex.models import Album, Artist, Track
from audiplex.routers import music
from tests.conftest import _create_test_app


@pytest.fixture(autouse=True)
def _fresh_cache():
    music._search_cache.update(sig=None, data=None)
    yield
    music._search_cache.update(sig=None, data=None)


def _add(engine, artist, album, title, n=1):
    s = sessionmaker(bind=engine)()
    a = s.query(Artist).filter(Artist.name == artist).first() or Artist(name=artist)
    s.add(a)
    s.flush()
    al = s.query(Album).filter(Album.title == album, Album.artist_id == a.id).first()
    if not al:
        al = Album(title=album, artist_id=a.id, folder_path=f"/m/{artist}/{album}")
        s.add(al)
        s.flush()
    s.add(Track(title=title, album_id=al.id, artist_id=a.id, disc_number=1, track_number=n,
                duration_seconds=100.0, file_path=f"/m/{artist}/{album}/{n}-{title}.mp3",
                file_size=1))
    s.commit()
    s.close()


@pytest.fixture
def seeded(db_engine):
    _add(db_engine, "The Beatles", "Abbey Road", "Help!", 1)
    _add(db_engine, "The Beatles", "Abbey Road", "Yesterday", 2)
    _add(db_engine, "Miles Davis", "Kind of Blue", "So What", 1)
    _add(db_engine, "Other", "Misc", "100% Pure", 1)
    _add(db_engine, "Other", "Misc", "100 Pure", 2)
    return db_engine


def get(client, q, **kw):
    r = client.get("/api/music/search", params={"q": q, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_word_and_across_fields(client, seeded):
    titles = [t["title"] for t in get(client, "beatles help")["tracks"]]
    assert titles[0] == "Help!"  # title + artist words
    assert [a["title"] for a in get(client, "davis blue")["albums"]][0] == "Kind of Blue"
    # album word + artist word hits every Beatles Abbey Road track
    assert "Yesterday" in [t["title"] for t in get(client, "abbey beatles")["tracks"]]


def test_case_insensitive(client, seeded):
    assert get(client, "BEATLES")["artists"][0]["name"] == "The Beatles"
    assert get(client, "hElP!")["tracks"][0]["title"] == "Help!"


def test_like_wildcards_literal(client, seeded):
    # "%" is literal: only the track with a literal percent sign matches in SQL
    assert [t["title"] for t in get(client, "100%")["tracks"]][0] == "100% Pure"
    # "_" is literal: "so_what" must not match "So What" in the SQL pass
    assert get(client, "so_what")["albums"] == []


def test_typo_finds_artist(client, seeded):
    assert "The Beatles" in [a["name"] for a in get(client, "beatels")["artists"]]


def test_short_query_empty(client, seeded):
    for q in ("", "a", " a "):
        assert get(client, q) == {"artists": [], "albums": [], "tracks": []}


def test_limit_respected(client, db_engine):
    for i in range(10):
        _add(db_engine, "Band", "Album", f"Love song {i}", i)
    assert len(get(client, "love", limit=3)["tracks"]) == 3
    assert client.get("/api/music/search", params={"q": "love", "limit": 500}).status_code == 422


def test_track_has_artist_name(client, seeded):
    assert get(client, "yesterday")["tracks"][0]["artist_name"] == "The Beatles"


def test_exact_ranks_first(client, db_engine):
    _add(db_engine, "X", "A", "A Love Supreme", 1)
    _add(db_engine, "X", "A", "Love Me Do", 2)
    _add(db_engine, "X", "A", "Love", 3)
    titles = [t["title"] for t in get(client, "love")["tracks"]]
    assert titles[:3] == ["Love", "Love Me Do", "A Love Supreme"]


def test_cache_reuse_and_invalidate(client, seeded, monkeypatch):
    calls = []
    real = music._build_search_cache
    monkeypatch.setattr(music, "_build_search_cache", lambda db: calls.append(1) or real(db))
    get(client, "beatels")
    get(client, "beatls")
    assert len(calls) == 1
    _add(seeded, "Zed", "Zap", "Zoom", 1)
    get(client, "beatels")
    assert len(calls) == 2


def test_requires_auth(db_engine):
    app = _create_test_app()
    Session = sessionmaker(bind=db_engine)

    def gdb():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = gdb
    with TestClient(app) as c:
        assert c.get("/api/music/search", params={"q": "beatles"}).status_code in (401, 403)
