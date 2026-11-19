"""#4217: the moov atom's byte range, found once and served on GET /books/{id}."""

import struct

from sqlalchemy.orm import sessionmaker

from audiplex.models import Book
from audiplex.mp4_index import find_moov


def _atom(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _write(path, *atoms):
    path.write_bytes(b"".join(atoms))
    return str(path)


def test_moov_at_end(tmp_path):
    ftyp, mdat, moov = _atom(b"ftyp", b"M4B " * 4), _atom(b"mdat", b"\0" * 1000), _atom(b"moov", b"x" * 300)
    p = _write(tmp_path / "a.m4b", ftyp, mdat, moov)
    assert find_moov(p) == (len(ftyp) + len(mdat), len(moov))


def test_moov_after_64bit_mdat(tmp_path):
    ftyp = _atom(b"ftyp", b"M4B ")
    body = b"\0" * 500
    mdat = struct.pack(">I4sQ", 1, b"mdat", 16 + len(body)) + body
    moov = _atom(b"moov", b"y" * 40)
    p = _write(tmp_path / "b.m4b", ftyp, mdat, moov)
    assert find_moov(p) == (len(ftyp) + len(mdat), len(moov))


def test_no_moov_or_corrupt_is_none(tmp_path):
    assert find_moov(_write(tmp_path / "c.m4b", _atom(b"ftyp", b"x"), _atom(b"mdat", b"z" * 10))) is None
    bad = struct.pack(">I4s", 9999, b"mdat") + b"short"  # size runs past EOF
    assert find_moov(_write(tmp_path / "d.m4b", _atom(b"ftyp", b"x"), bad)) is None
    assert find_moov(str(tmp_path / "missing.m4b")) is None


def _add_book(db_engine, path, **kw):
    session = sessionmaker(bind=db_engine)()
    book = Book(title="B", file_path=path, file_size=1, file_hash=path, duration_seconds=1.0, **kw)
    session.add(book)
    session.commit()
    book_id = book.id
    session.close()
    return book_id


def test_book_detail_fills_and_keeps_moov(client, db_engine, tmp_path):
    ftyp, mdat, moov = _atom(b"ftyp", b"M4B "), _atom(b"mdat", b"\0" * 64), _atom(b"moov", b"m" * 20)
    path = _write(tmp_path / "e.m4b", ftyp, mdat, moov)
    book_id = _add_book(db_engine, path)
    data = client.get(f"/api/library/books/{book_id}").json()
    assert (data["moov_offset"], data["moov_size"]) == (len(ftyp) + len(mdat), len(moov))
    session = sessionmaker(bind=db_engine)()
    assert session.get(Book, book_id).moov_offset == len(ftyp) + len(mdat)  # stored, not re-walked
    session.close()


def test_book_detail_moov_null_when_unknown(client, db_engine, tmp_path):
    mp3 = tmp_path / "f.mp3"
    mp3.write_bytes(b"ID3")
    book_id = _add_book(db_engine, str(mp3))
    data = client.get(f"/api/library/books/{book_id}").json()
    assert data["moov_offset"] is None and data["moov_size"] is None
