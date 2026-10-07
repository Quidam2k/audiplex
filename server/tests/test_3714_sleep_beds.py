"""#3714: the sleep button's bed list."""

from audiplex.models import Book


def _book(db, title, n):
    db.add(Book(title=title, file_path=f"/b/{n}.m4b", category="meditation"))


def test_beds_default_first_and_unmatched_left_out(client, db_session):
    _book(db_session, "Brown Noise - Sleep Loop", 1)
    _book(db_session, "Dune", 2)
    _book(db_session, "Starship Sleeping Quarters", 3)
    db_session.commit()

    beds = client.get("/api/library/sleep-beds").json()

    assert [b["title"] for b in beds] == ["Starship Sleeping Quarters", "Brown Noise - Sleep Loop"]
    assert beds[0]["stream_url"] == f"/api/stream/{beds[0]['id']}"


def test_no_beds_is_an_empty_list(client):
    assert client.get("/api/library/sleep-beds").json() == []
