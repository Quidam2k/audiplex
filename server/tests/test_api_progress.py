"""Tests for progress tracking endpoints."""

import pytest
from sqlalchemy.orm import sessionmaker

from audiplex.models import Book, Favorite, PlaybackPosition, User


class TestProgress:
    def test_get_no_progress(self, client, db_engine, sample_book):
        resp = client.get(f"/api/progress/{sample_book.id}")
        assert resp.status_code == 404

    def test_update_and_get(self, client, db_engine, sample_book):
        # Update progress
        resp = client.put(
            f"/api/progress/{sample_book.id}",
            json={"position_seconds": 120.5, "chapter_index": 1},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["position_seconds"] == 120.5
        assert data["chapter_index"] == 1
        assert data["is_finished"] is False

        # Get it back
        resp = client.get(f"/api/progress/{sample_book.id}")
        assert resp.status_code == 200
        assert resp.json()["position_seconds"] == 120.5

    def test_update_overwrites(self, client, db_engine, sample_book):
        client.put(
            f"/api/progress/{sample_book.id}",
            json={"position_seconds": 100.0},
        )
        client.put(
            f"/api/progress/{sample_book.id}",
            json={"position_seconds": 200.0, "chapter_index": 2},
        )

        resp = client.get(f"/api/progress/{sample_book.id}")
        assert resp.json()["position_seconds"] == 200.0
        assert resp.json()["chapter_index"] == 2

    def test_mark_finished(self, client, db_engine, sample_book):
        client.put(
            f"/api/progress/{sample_book.id}",
            json={"position_seconds": 3600.0, "is_finished": True},
        )
        resp = client.get(f"/api/progress/{sample_book.id}")
        assert resp.json()["is_finished"] is True

    def test_list_in_progress(self, client, db_engine):
        """GET /api/progress returns only non-finished books."""
        Session = sessionmaker(bind=db_engine)
        session = Session()
        uid = session.query(User).first().id
        b1 = Book(title="Book 1", file_path="/b1.m4b", file_size=100, file_hash="h1")
        b2 = Book(title="Book 2", file_path="/b2.m4b", file_size=100, file_hash="h2")
        session.add_all([b1, b2])
        session.flush()

        session.add(PlaybackPosition(book_id=b1.id, user_id=uid, position_seconds=50.0))
        session.add(PlaybackPosition(book_id=b2.id, user_id=uid, position_seconds=100.0, is_finished=True))
        session.commit()
        b1_id = b1.id
        session.close()

        resp = client.get("/api/progress")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["book_id"] == b1_id

    def test_update_nonexistent_book(self, client):
        resp = client.put(
            "/api/progress/999",
            json={"position_seconds": 50.0},
        )
        assert resp.status_code == 404

    def test_finishing_book_clears_to_read(self, client, db_engine, sample_book):
        """Marking a book finished should auto-remove it from to_read favorites."""
        Session = sessionmaker(bind=db_engine)
        session = Session()
        uid = session.query(User).first().id
        session.add(Favorite(entity_type="to_read", entity_key=str(sample_book.id), user_id=uid))
        session.add(Favorite(entity_type="book", entity_key=str(sample_book.id), user_id=uid))
        session.commit()
        session.close()

        client.put(
            f"/api/progress/{sample_book.id}",
            json={"position_seconds": 3600.0, "is_finished": True},
        )

        Session = sessionmaker(bind=db_engine)
        session = Session()
        remaining = session.query(Favorite).all()
        types = sorted(f.entity_type for f in remaining)
        assert types == ["book"]
        session.close()

    def test_unfinished_update_keeps_to_read(self, client, db_engine, sample_book):
        Session = sessionmaker(bind=db_engine)
        session = Session()
        uid = session.query(User).first().id
        session.add(Favorite(entity_type="to_read", entity_key=str(sample_book.id), user_id=uid))
        session.commit()
        session.close()

        client.put(
            f"/api/progress/{sample_book.id}",
            json={"position_seconds": 100.0, "is_finished": False},
        )

        Session = sessionmaker(bind=db_engine)
        session = Session()
        assert session.query(Favorite).count() == 1
        session.close()
