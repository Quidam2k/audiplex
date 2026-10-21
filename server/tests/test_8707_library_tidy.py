"""#8707 library tidy: chapter-split Libation folders become one book filled from
Libation's DB, and mix-folder copies of a recording drop out of the flat lists."""

import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from audiplex.config import LibraryRoot
from audiplex.dedup import canonical_rank, mark_duplicates
from audiplex.libation import asin_from_folder, load_index
from audiplex.models import Album, Artist, Book, Chapter, PlaybackPosition, Track
from audiplex.scanner import scan_library

ASIN = "B0934GTSGT"


@pytest.fixture
def libation_db(tmp_path, monkeypatch):
    path = tmp_path / "LibationContext.db"
    c = sqlite3.connect(path)
    c.executescript(f"""
        CREATE TABLE Books(BookId INTEGER, AudibleProductId TEXT, Title TEXT, Subtitle TEXT);
        CREATE TABLE Contributors(ContributorId INTEGER, AudibleContributorId TEXT, Name TEXT);
        CREATE TABLE BookContributor(BookId INTEGER, ContributorId INTEGER, Role INTEGER, "Order" INTEGER);
        CREATE TABLE Series(SeriesId INTEGER, AudibleSeriesId TEXT, Name TEXT);
        CREATE TABLE SeriesBook(SeriesId INTEGER, BookId INTEGER, "Order" TEXT);
        INSERT INTO Books VALUES (1, '{ASIN}', 'Carl''s Doomsday Scenario', 'Dungeon Crawler Carl, Book 2');
        INSERT INTO Contributors VALUES (1, 'a', 'Matt Dinniman'), (2, 'n', 'Jeff Hays');
        INSERT INTO BookContributor VALUES (1, 1, 1, 0), (1, 2, 2, 0);
        INSERT INTO Series VALUES (1, 's', 'Dungeon Crawler Carl');
        INSERT INTO SeriesBook VALUES (1, 1, '2');
    """)
    c.commit()
    c.close()
    monkeypatch.setenv("AUDIPLEX_LIBATION_DB", str(path))
    return path


def _fake_track(path):
    return {"duration": 100.0, "title": path.stem.split(" - ")[-1], "album": "tag album",
            "artist": "tag artist", "albumartist": None, "composer": None}


def _scan(db, root):
    with (
        patch("audiplex.scanners.audiobook_misc._read_track", side_effect=_fake_track),
        patch("audiplex.scanners.audiobook_misc._populate_cover", return_value=False),
    ):
        return scan_library(db, [LibraryRoot(path=str(root))], str(root / "covers"))


def test_asin_from_folder():
    assert asin_from_folder(f"Matt DinnimanCarl's Doomsday Scenario [{ASIN.lower()}]") == ASIN
    assert asin_from_folder("No id here") is None


def test_load_index_reads_libation(libation_db):
    lb = load_index()[ASIN]
    assert (lb.title, lb.authors, lb.narrators) == ("Carl's Doomsday Scenario", "Matt Dinniman", "Jeff Hays")
    assert (lb.series, lb.series_sequence) == ("Dungeon Crawler Carl", "2")


def test_missing_libation_db_is_empty(tmp_path):
    assert load_index(tmp_path / "nope.db") == {}


def test_chapter_split_folder_is_one_book(db_session, tmp_path, libation_db):
    root = tmp_path / "Books"
    folder = root / f"Matt DinnimanCarl's Doomsday Scenario [{ASIN}]"
    folder.mkdir(parents=True)
    for n, name in enumerate(["Opening Credits", "Chapter 1", "Chapter 2"], 1):
        (folder / f"Carl [{ASIN}] - {n:02d} - {name}.m4b").write_bytes(b"x")

    _scan(db_session, root)

    books = db_session.query(Book).all()
    assert len(books) == 1
    book = books[0]
    assert book.category == "audiobook_misc"
    assert book.file_path == str(folder)
    assert (book.title, book.author, book.narrator) == ("Carl's Doomsday Scenario", "Matt Dinniman", "Jeff Hays")
    assert (book.series, book.series_sequence, book.series_source) == ("Dungeon Crawler Carl", "2", "libation")
    titles = [c.title for c in db_session.query(Chapter).order_by(Chapter.index)]
    assert titles == ["Opening Credits", "Chapter 1", "Chapter 2"]


def test_old_chapter_file_positions_move_to_folder_book(db_session, tmp_path, libation_db):
    root = tmp_path / "Books"
    folder = root / f"Carl [{ASIN}]"
    folder.mkdir(parents=True)
    files = [folder / f"Carl - {n:02d} - Part {n}.m4b" for n in (1, 2)]
    for f in files:
        f.write_bytes(b"x")
    # The pre-#8707 shape: one Book row per chapter file, with a position on part 2.
    old = Book(title="2 - Part 2", file_path=str(files[1]), category="audiobook_clean")
    db_session.add(old)
    db_session.flush()
    db_session.add(PlaybackPosition(book_id=old.id, user_id=None, position_seconds=30.0, chapter_index=0))
    db_session.commit()

    _scan(db_session, root)

    book = db_session.query(Book).one()
    pos = db_session.query(PlaybackPosition).one()
    assert pos.book_id == book.id
    assert pos.position_seconds == pytest.approx(130.0)  # part 1 is 100 s long
    assert pos.chapter_index == 1


def test_single_mp3_folder_is_indexed(db_session, tmp_path, libation_db):
    root = tmp_path / "Books"
    folder = root / "Ginkgo Biloba [B098B9BYKT]"
    folder.mkdir(parents=True)
    (folder / "Ginkgo Biloba [B098B9BYKT].mp3").write_bytes(b"x")
    _scan(db_session, root)
    book = db_session.query(Book).one()
    assert book.category == "audiobook_misc"
    assert book.title == "tag album"  # not in Libation: tags stand


def test_canonical_rank_prefers_real_album():
    real = r"H:\Stacked Deck\Music\Artists & Albums\World\Adiemus\09 - Cantus.mp3"
    mix = r"Q:\Stacked Deck Music\Bike Music\09 - Cantus.mp3"
    ren = r"Q:\Stacked Deck Music\Artists & Albums\Mixed\Ren Faire\09 - Cantus.mp3"
    assert canonical_rank(real, 9) < canonical_rank(mix, 1)
    assert canonical_rank(mix, 1) < canonical_rank(ren, 2)


def test_duplicates_hidden_from_flat_lists_not_albums(client, db_session):
    artist = Artist(name="Adiemus")
    db_session.add(artist)
    db_session.flush()
    mix = Album(title="Bike Music", artist_id=artist.id, folder_path=r"Q:\Stacked Deck Music\Bike Music")
    real = Album(title="Cantata Mundi", artist_id=artist.id,
                 folder_path=r"H:\Stacked Deck\Music\Artists & Albums\World\Adiemus")
    db_session.add_all([mix, real])
    db_session.flush()
    mix_copy = Track(title="Cantus", album_id=mix.id, artist_id=artist.id, duration_seconds=240.0,
                     file_path=mix.folder_path + r"\09 - Cantus.mp3")
    real_copy = Track(title="Cantus", album_id=real.id, artist_id=artist.id, duration_seconds=241.0,
                      file_path=real.folder_path + r"\09 - Cantus.mp3")
    db_session.add_all([mix_copy, real_copy])
    db_session.flush()

    stats = mark_duplicates(db_session)
    db_session.commit()

    assert stats["groups"] == 1 and stats["hidden"] == 1
    db_session.refresh(mix_copy)
    assert mix_copy.duplicate_of == real_copy.id
    ids = [t["id"] for t in client.get(f"/api/music/artists/{artist.id}/tracks").json()]
    assert ids == [real_copy.id]
    assert [t["id"] for t in client.get(f"/api/music/albums/{mix.id}").json()["tracks"]] == [mix_copy.id]
    assert [t["id"] for t in client.get("/api/music/search", params={"q": "Cantus"}).json()["tracks"]] == [real_copy.id]
