"""Tests for audio streaming endpoints."""

import pytest
from sqlalchemy.orm import sessionmaker

from audiplex.models import Book, Chapter


def _insert_book_with_real_file(db_engine, file_path, file_size):
    """Insert a book pointing to a real file on disk."""
    Session = sessionmaker(bind=db_engine)
    session = Session()
    book = Book(
        title="Stream Test",
        file_path=file_path,
        file_size=file_size,
        file_hash="streamhash",
    )
    session.add(book)
    session.commit()
    book_id = book.id
    session.close()
    return book_id


def _insert_misc_book_with_track(db_engine, folder_path: str, track_path: str, file_size: int):
    """Insert a misc Book + one Chapter pointing at a real on-disk track."""
    Session = sessionmaker(bind=db_engine)
    session = Session()
    book = Book(
        title="Misc Stream",
        category="audiobook_misc",
        file_path=folder_path,
        file_size=file_size,
        file_hash="mischash",
        duration_seconds=60.0,
    )
    session.add(book)
    session.flush()
    session.add(Chapter(
        book_id=book.id,
        index=0,
        title="Track 1",
        start_seconds=0.0,
        end_seconds=60.0,
        file_path=track_path,
    ))
    session.commit()
    book_id = book.id
    session.close()
    return book_id


class TestStreamAudio:
    def test_full_file(self, client, db_engine, audio_file_on_disk):
        file_path, file_size = audio_file_on_disk
        book_id = _insert_book_with_real_file(db_engine, file_path, file_size)

        resp = client.get(f"/api/stream/{book_id}")
        assert resp.status_code == 200
        assert resp.headers["accept-ranges"] == "bytes"
        assert int(resp.headers["content-length"]) == file_size
        assert len(resp.content) == file_size

    def test_range_request(self, client, db_engine, audio_file_on_disk):
        file_path, file_size = audio_file_on_disk
        book_id = _insert_book_with_real_file(db_engine, file_path, file_size)

        resp = client.get(
            f"/api/stream/{book_id}",
            headers={"Range": "bytes=0-1023"},
        )
        assert resp.status_code == 206
        assert len(resp.content) == 1024
        assert resp.headers["content-range"] == f"bytes 0-1023/{file_size}"

    def test_range_open_end(self, client, db_engine, audio_file_on_disk):
        file_path, file_size = audio_file_on_disk
        book_id = _insert_book_with_real_file(db_engine, file_path, file_size)

        start = file_size - 100
        resp = client.get(
            f"/api/stream/{book_id}",
            headers={"Range": f"bytes={start}-"},
        )
        assert resp.status_code == 206
        assert len(resp.content) == 100

    def test_range_suffix(self, client, db_engine, audio_file_on_disk):
        """bytes=-N means last N bytes."""
        file_path, file_size = audio_file_on_disk
        book_id = _insert_book_with_real_file(db_engine, file_path, file_size)

        resp = client.get(
            f"/api/stream/{book_id}",
            headers={"Range": "bytes=-200"},
        )
        assert resp.status_code == 206
        assert len(resp.content) == 200

    def test_invalid_range(self, client, db_engine, audio_file_on_disk):
        file_path, file_size = audio_file_on_disk
        book_id = _insert_book_with_real_file(db_engine, file_path, file_size)

        resp = client.get(
            f"/api/stream/{book_id}",
            headers={"Range": f"bytes={file_size + 100}-{file_size + 200}"},
        )
        assert resp.status_code == 416

    def test_book_not_found(self, client):
        resp = client.get("/api/stream/999")
        assert resp.status_code == 404

    def test_file_missing_from_disk(self, client, db_engine):
        """Book exists in DB but file was deleted from disk."""
        book_id = _insert_book_with_real_file(db_engine, "/nonexistent/fake.m4b", 1000)
        resp = client.get(f"/api/stream/{book_id}")
        assert resp.status_code == 404


class TestStreamTrack:
    def test_returns_audio_for_misc_book(self, client, db_engine, audio_file_on_disk):
        track_path, file_size = audio_file_on_disk
        book_id = _insert_misc_book_with_track(db_engine, "/fake/folder", track_path, file_size)

        resp = client.get(f"/api/stream/{book_id}/track/0")
        assert resp.status_code == 200
        assert resp.headers["accept-ranges"] == "bytes"
        assert int(resp.headers["content-length"]) == file_size
        assert len(resp.content) == file_size

    def test_404_for_libation_book(self, client, db_engine, audio_file_on_disk):
        """Track endpoint refuses books that aren't audiobook_misc."""
        file_path, file_size = audio_file_on_disk
        book_id = _insert_book_with_real_file(db_engine, file_path, file_size)
        resp = client.get(f"/api/stream/{book_id}/track/0")
        assert resp.status_code == 404

    def test_track_endpoint_range_request(self, client, db_engine, audio_file_on_disk):
        track_path, file_size = audio_file_on_disk
        book_id = _insert_misc_book_with_track(db_engine, "/fake/folder", track_path, file_size)

        resp = client.get(
            f"/api/stream/{book_id}/track/0",
            headers={"Range": "bytes=0-1023"},
        )
        assert resp.status_code == 206
        assert len(resp.content) == 1024
        assert resp.headers["content-range"] == f"bytes 0-1023/{file_size}"

    def test_legacy_endpoint_404_for_misc_book(self, client, db_engine):
        """Legacy /api/stream/{id} refuses misc books with helpful message."""
        Session = sessionmaker(bind=db_engine)
        session = Session()
        book = Book(
            title="M",
            category="audiobook_misc",
            file_path="/fake/folder",
            file_size=0,
            file_hash="x",
        )
        session.add(book)
        session.commit()
        book_id = book.id
        session.close()

        resp = client.get(f"/api/stream/{book_id}")
        assert resp.status_code == 404
        assert "track" in resp.json()["detail"].lower()
