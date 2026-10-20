"""#3385: the library audit is read-only and ranks what looks off by size."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import library_audit as la  # noqa: E402


def _db(path, books=(), tracks=(), albums=(), chapters=()):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE books(id INTEGER PRIMARY KEY, title, author, narrator, category,
            duration_seconds, file_path, cue_path, has_cover, file_size, file_hash);
        CREATE TABLE chapters(id INTEGER PRIMARY KEY, book_id, file_path);
        CREATE TABLE albums(id INTEGER PRIMARY KEY, title, artist_id, folder_path, has_cover, track_count);
        CREATE TABLE artists(id INTEGER PRIMARY KEY, name);
        CREATE TABLE tracks(id INTEGER PRIMARY KEY, title, album_id, artist_id, file_path,
            file_size, file_hash, duration_seconds, content_kind);
    """)
    c.executemany("INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?,?,?)", books)
    c.executemany("INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?,?)", tracks)
    c.executemany("INSERT INTO albums VALUES (?,?,?,?,?,?)", albums)
    c.executemany("INSERT INTO chapters VALUES (?,?,?)", chapters)
    c.commit()
    c.close()


def _write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return str(path)


def test_audit_end_to_end(tmp_path):
    lib = tmp_path / "lib"
    a = _write(lib / "Rock" / "Album" / "01 Song.mp3", 1000)
    b = _write(lib / "Jazz" / "Album" / "01 Song.mp3", 1000)
    book = _write(lib / "Books" / "Dune" / "Dune.m4b", 50)
    _write(lib / "Loose" / "a.mp3", 300)
    _write(lib / "Loose" / "b.mp3", 200)
    _write(lib / "Rock" / "Album" / "cover.jpg", 10)
    _write(lib / "Rock" / "Album" / "setup.exe", 70)
    (lib / "Empty" / "Deeper").mkdir(parents=True)
    gone = str(lib / "Rock" / "Album" / "02 Gone.mp3")
    offline = "Z:/offline/lost.mp3"
    db = tmp_path / "a.db"
    _db(db,
        books=[(1, "Dune", None, None, "audiobook_clean", 3600, book, None, 0, 50, "h1")],
        tracks=[(1, "Song", 1, 1, a, 1000, None, 1, "music"),
                (2, "Song", 2, 1, b, 1000, None, 1, "music"),
                (3, "Gone", 1, 1, gone, 400, None, 1, "music"),
                (4, "Lost", 1, 1, offline, 400, None, 1, "music")],
        albums=[(1, "Album", 1, str(lib / "Rock" / "Album"), 0, 2)])
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"library_roots:\n- category: music\n  path: '{lib}'\n"
                   "- category: music\n  path: Z:/offline\n", encoding="utf-8")
    before = db.read_bytes()
    out_md, out_json = tmp_path / "r.md", tmp_path / "r.json"

    assert la.main(["--db", str(db), "--config", str(cfg),
                    "--out-md", str(out_md), "--out-json", str(out_json)]) == 0

    assert db.read_bytes() == before
    import json
    r = json.loads(out_json.read_text(encoding="utf-8"))
    s = r["sections"]
    assert s["duplicate_files"]["count"] == 1 and s["duplicate_files"]["bytes"] == 1000
    assert s["duplicate_files"]["items"][0]["kind"] == "likely"
    assert [i["id"] for i in s["missing_files"]["items"]] == [3]  # not the offline row
    assert r["not_checked"] == 1
    assert s["orphan_audio"]["count"] == 2 and s["orphan_audio"]["bytes"] == 500
    assert s["orphan_audio"]["items"][0]["folder"].endswith("Loose")
    assert [i["extension"] for i in s["stray_files"]["items"]] == [".exe"]
    assert s["books_missing_metadata"]["items"][0]["flags"] == ["no author", "no cover", "no chapters"]
    assert any(p.endswith("Empty") for p in (i["path"] for i in s["empty_folders"]["items"]))

    md = out_md.read_text(encoding="utf-8")
    for word in ("delete", "remove", "recommend"):
        assert word not in md.lower()
    order = [line.split(". ", 1)[1] for line in md.splitlines() if line.startswith("## ")]
    assert order[0] == la.SECTION_NAMES["duplicate_files"]  # 1000 B beats 500 B orphans


def test_refuses_report_inside_a_root(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    db = tmp_path / "a.db"
    _db(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"library_roots:\n- path: '{lib}'\n", encoding="utf-8")
    assert la.main(["--db", str(db), "--config", str(cfg), "--out-md", str(lib / "r.md")]) == 1
    assert not (lib / "r.md").exists()
