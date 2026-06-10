"""Tests for library API endpoints."""

import pytest

from audiplex.models import Book, Chapter


class TestListBooks:
    def test_empty_library(self, client):
        resp = client.get("/api/library/books")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_books(self, client, db_engine, sample_book):
        resp = client.get("/api/library/books")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Test Book"
        assert data[0]["author"] == "Test Author"

    def test_books_include_category(self, client, sample_book):
        resp = client.get("/api/library/books")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["category"] == "audiobook_clean"

    def test_filter_by_author(self, client, db_engine):
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=db_engine)
        session = Session()
        session.add(Book(title="A", author="Alice", file_path="/a.m4b", file_size=100, file_hash="a"))
        session.add(Book(title="B", author="Bob", file_path="/b.m4b", file_size=100, file_hash="b"))
        session.commit()
        session.close()

        resp = client.get("/api/library/books?author=Alice")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "A"

    def test_filter_by_series(self, client, db_engine):
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=db_engine)
        session = Session()
        session.add(Book(title="X", series="Saga", file_path="/x.m4b", file_size=100, file_hash="x"))
        session.add(Book(title="Y", series="Other", file_path="/y.m4b", file_size=100, file_hash="y"))
        session.commit()
        session.close()

        resp = client.get("/api/library/books?series=Saga")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "X"

    def test_filter_books_by_category(self, client, db_engine):
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=db_engine)
        session = Session()
        session.add(Book(
            title="Clean Book", category="audiobook_clean",
            file_path="/c.m4b", file_size=100, file_hash="c",
        ))
        session.add(Book(
            title="Misc Book", category="audiobook_misc",
            file_path="/m", file_size=100, file_hash="m",
        ))
        session.commit()
        session.close()

        resp = client.get("/api/library/books?category=audiobook_misc")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Misc Book"
        assert data[0]["category"] == "audiobook_misc"


class TestGetBook:
    def test_found(self, client, db_engine, sample_book):
        resp = client.get(f"/api/library/books/{sample_book.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Test Book"
        assert len(data["chapters"]) == 5
        assert data["category"] == "audiobook_clean"
        assert data["track_urls"] == []

    def test_not_found(self, client):
        resp = client.get("/api/library/books/999")
        assert resp.status_code == 404

    def test_misc_book_detail_has_track_urls(self, client, db_engine):
        """audiobook_misc book exposes per-track stream URLs."""
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=db_engine)
        session = Session()
        book = Book(
            title="Misc Book",
            category="audiobook_misc",
            file_path="/fake/folder",
            file_size=0,
            file_hash="m1",
            duration_seconds=300.0,
        )
        session.add(book)
        session.flush()
        for i in range(3):
            session.add(Chapter(
                book_id=book.id,
                index=i,
                title=f"Track {i + 1}",
                start_seconds=i * 100.0,
                end_seconds=(i + 1) * 100.0,
                file_path=f"/fake/folder/track{i}.mp3",
            ))
        session.commit()
        book_id = book.id
        session.close()

        resp = client.get(f"/api/library/books/{book_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["category"] == "audiobook_misc"
        assert data["track_urls"] == [
            f"/api/stream/{book_id}/track/0",
            f"/api/stream/{book_id}/track/1",
            f"/api/stream/{book_id}/track/2",
        ]


class TestAuthors:
    def test_list_authors(self, client, db_engine):
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=db_engine)
        session = Session()
        session.add(Book(title="A1", author="Alice", file_path="/a1.m4b", file_size=100, file_hash="a1"))
        session.add(Book(title="A2", author="Alice", file_path="/a2.m4b", file_size=100, file_hash="a2"))
        session.add(Book(title="B1", author="Bob", file_path="/b1.m4b", file_size=100, file_hash="b1"))
        session.commit()
        session.close()

        resp = client.get("/api/library/authors")
        data = resp.json()
        assert len(data) == 2
        alice = next(a for a in data if a["name"] == "Alice")
        assert alice["book_count"] == 2

    def test_filter_authors_by_category(self, client, db_engine):
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=db_engine)
        session = Session()
        session.add(Book(
            title="A1", author="Alice", category="audiobook_clean",
            file_path="/a1.m4b", file_size=100, file_hash="a1",
        ))
        session.add(Book(
            title="B1", author="Bob", category="audiobook_misc",
            file_path="/b1", file_size=100, file_hash="b1",
        ))
        session.commit()
        session.close()

        resp = client.get("/api/library/authors?category=audiobook_misc")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Bob"


class TestSeries:
    def test_list_series(self, client, db_engine):
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=db_engine)
        session = Session()
        session.add(Book(title="S1", series="Epic", file_path="/s1.m4b", file_size=100, file_hash="s1"))
        session.add(Book(title="S2", series="Epic", file_path="/s2.m4b", file_size=100, file_hash="s2"))
        session.add(Book(title="S3", series=None, file_path="/s3.m4b", file_size=100, file_hash="s3"))
        session.commit()
        session.close()

        resp = client.get("/api/library/series")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Epic"
        assert data[0]["book_count"] == 2

    def test_filter_series_by_category(self, client, db_engine):
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=db_engine)
        session = Session()
        session.add(Book(
            title="S1", series="Epic", category="audiobook_clean",
            file_path="/s1.m4b", file_size=100, file_hash="s1",
        ))
        session.add(Book(
            title="S2", series="Lectures", category="audiobook_misc",
            file_path="/s2", file_size=100, file_hash="s2",
        ))
        session.commit()
        session.close()

        resp = client.get("/api/library/series?category=audiobook_misc")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Lectures"
