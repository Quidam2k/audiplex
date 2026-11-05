"""GET /api/files/{kind}/{id}: admin-only file location for the long-press sheet (#5485)."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from audiplex.auth import get_current_user, hash_password
from audiplex.database import get_db
from audiplex.models import Book, User
from audiplex.routers import files


def _client(db_engine, admin: bool) -> TestClient:
    Session_ = sessionmaker(bind=db_engine)
    with Session_() as s:
        s.add(User(username="u", password_hash=hash_password("pw"), display_name="U", is_admin=admin))
        s.commit()
    app = FastAPI()
    app.include_router(files.router)

    def db():
        s = Session_()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda d=Depends(get_db): d.query(User).filter_by(username="u").one()
    return TestClient(app)


def test_book_folder_and_name(db_engine, sample_book):
    r = _client(db_engine, admin=True).get(f"/api/files/book/{sample_book.id}")
    assert r.status_code == 200
    assert r.json() == {"folder": "/fake/path", "file_name": "test.m4b"}


def test_backslash_meditation_path(db_engine, db_session):
    book = Book(title="Calm", file_path=r"med\audio\Calm.m4a", category="meditation",
                has_cover=False, file_size=1, file_hash="h")
    db_session.add(book)
    db_session.commit()
    r = _client(db_engine, admin=True).get(f"/api/files/book/{book.id}")
    assert r.json() == {"folder": r"med\audio", "file_name": "Calm.m4a"}


def test_track_and_album(db_engine, sample_track):
    c = _client(db_engine, admin=True)
    assert c.get(f"/api/files/track/{sample_track.id}").json() == {
        "folder": "/fake/music/Test Artist/Test Album", "file_name": "01 Test Track.mp3"}
    assert c.get(f"/api/files/album/{sample_track.album_id}").json() == {
        "folder": "/fake/music/Test Artist/Test Album", "file_name": None}


def test_non_admin_forbidden(db_engine, sample_book):
    assert _client(db_engine, admin=False).get(f"/api/files/book/{sample_book.id}").status_code == 403


@pytest.mark.parametrize("url", ["/api/files/book/99999", "/api/files/playlist/1"])
def test_unknown_is_404(db_engine, url):
    assert _client(db_engine, admin=True).get(url).status_code == 404
