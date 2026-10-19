"""#4059: GET /api/playback/book-position — the owner's place in an audiobook, for transcript lookup."""

from datetime import datetime, timedelta, timezone

import pytest

from audiplex.models import Book, PlaybackPosition, User
from audiplex.playback_bus import bus
from audiplex.routers import playback


@pytest.fixture(autouse=True)
def owner_is_testuser(monkeypatch):
    s = playback.get_settings().model_copy(update={"dj_owner_username": "testuser"})
    monkeypatch.setattr(playback, "get_settings", lambda: s)
    bus.reset()
    yield
    bus.reset()


def _bookmark(db, book, seconds, minutes_ago, finished=False):
    owner = db.query(User).filter(User.username == "testuser").one()
    db.add(PlaybackPosition(book_id=book.id, user_id=owner.id, position_seconds=seconds, chapter_index=1,
                            is_finished=finished,
                            updated_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))
    db.commit()


def test_404_when_owner_has_no_book(client):
    assert client.get("/api/playback/book-position").status_code == 404


def test_latest_unfinished_bookmark_when_nothing_on_device(client, db_session, sample_book):
    other = Book(title="Other", author="A", duration_seconds=10, file_path="/fake/other.m4b")
    done = Book(title="Done", author="A", duration_seconds=10, file_path="/fake/done.m4b")
    db_session.add_all([other, done])
    db_session.commit()
    _bookmark(db_session, other, 50.0, minutes_ago=60)
    _bookmark(db_session, sample_book, 1234.5, minutes_ago=5)
    _bookmark(db_session, done, 9.0, minutes_ago=1, finished=True)
    got = client.get("/api/playback/book-position").json()
    assert got["book_id"] == sample_book.id and got["title"] == "Test Book"
    assert got["position_s"] == 1234.5 and got["chapter_index"] == 1
    assert got["playing"] is False and got["source"] == "bookmark"


def test_device_book_wins_over_bookmark(client, db_session, sample_book):
    _bookmark(db_session, sample_book, 10.0, minutes_ago=5)
    bus.set_state({"playing": True, "track": None, "position_ms": 754_250,
                   "book": {"id": sample_book.id, "title": "Test Book", "chapter_index": 2}})
    got = client.get("/api/playback/book-position").json()
    assert got["position_s"] == 754.25 and got["chapter_index"] == 2
    assert got["playing"] is True and got["source"] == "device"


def test_music_on_device_falls_back_to_bookmark(client, db_session, sample_book):
    _bookmark(db_session, sample_book, 42.0, minutes_ago=5)
    bus.set_state({"playing": True, "track": {"id": 7}, "position_ms": 1000})
    got = client.get("/api/playback/book-position").json()
    assert got["position_s"] == 42.0 and got["source"] == "bookmark"
