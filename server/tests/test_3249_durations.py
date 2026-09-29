"""#3249: duration truth — the audit script and the short-'complete' note.

Track 986 says 839 s in the DB; the file is 168 s. The script finds such rows
and fixes ONLY duration_seconds, and only after a backup exists. A 'complete'
whose player duration disagrees with the DB is logged once.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from audiplex import playback_bus
from audiplex.models import Track
from audiplex.routers import music

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_track_durations as audit  # noqa: E402


@pytest.fixture
def lib(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, file_path TEXT,"
                " duration_seconds REAL, content_kind TEXT, album_id INTEGER)")
    files = {}
    for name in ("good", "bad"):
        f = tmp_path / f"{name}.mp3"
        f.write_bytes(b"x")
        files[name] = str(f)
    con.executemany("INSERT INTO tracks VALUES (?,?,?,?,?,?)", [
        (1, "good", files["good"], 200.0, "music", 7),
        (2, "bad", files["bad"], 839.0, "music", 7),
        (3, "gone", str(tmp_path / "nope.mp3"), 100.0, "music", 7),
    ])
    con.commit()
    con.close()
    real = {files["good"]: 202.0, files["bad"]: 167.8}
    monkeypatch.setattr(audit, "probe", lambda ffprobe, p: (real[p], ""))
    return db


def _durations(db):
    con = sqlite3.connect(db)
    out = dict(con.execute("SELECT id, duration_seconds FROM tracks").fetchall())
    other = con.execute("SELECT title, album_id FROM tracks WHERE id=2").fetchone()
    con.close()
    return out, other


def test_report_only_changes_nothing(lib, tmp_path):
    rep = tmp_path / "r.json"
    assert audit.main(["--db", str(lib), "--report", str(rep)]) == 0
    r = json.loads(rep.read_text())
    assert [m["id"] for m in r["mismatched"]] == [2]   # 200 vs 202 is within tolerance
    assert r["missing"] == [3] and r["applied"] is False
    assert _durations(lib)[0] == {1: 200.0, 2: 839.0, 3: 100.0}


def test_apply_refuses_without_backup(lib, tmp_path):
    assert audit.main(["--db", str(lib), "--apply", "--report", str(tmp_path / "r.json")]) == 2
    assert audit.main(["--db", str(lib), "--apply", "--backup-done-at", str(tmp_path / "none"),
                       "--report", str(tmp_path / "r.json")]) == 2
    assert _durations(lib)[0][2] == 839.0


def test_apply_fixes_only_the_duration_column(lib, tmp_path):
    bak = tmp_path / "a.db.bak"
    bak.write_bytes(lib.read_bytes())
    rep = tmp_path / "r.json"
    assert audit.main(["--db", str(lib), "--apply", "--backup-done-at", str(bak),
                       "--report", str(rep)]) == 0
    durs, other = _durations(lib)
    assert durs == {1: 200.0, 2: 167.8, 3: 100.0}
    assert other == ("bad", 7)
    assert json.loads(rep.read_text())["backup"] == str(bak)


def test_short_complete_is_logged_once(client, db_session, sample_album, monkeypatch, capsys):
    monkeypatch.setattr(music, "_duration_mismatch_logged", set())
    t = Track(title="Serenity", album_id=sample_album.id, artist_id=sample_album.artist_id,
              file_path="/m/s.mp3", file_size=1, duration_seconds=839.0)
    ok = Track(title="Fine", album_id=sample_album.id, artist_id=sample_album.artist_id,
               file_path="/m/f.mp3", file_size=1, duration_seconds=200.0)
    db_session.add_all([t, ok])
    db_session.commit()
    for _ in range(2):
        assert client.post("/api/music/stats", json={
            "track_id": t.id, "event": "complete", "played_seconds": 167.8}).status_code == 200
    client.post("/api/music/stats", json={"track_id": ok.id, "event": "complete",
                                          "played_seconds": 201.0})
    assert capsys.readouterr().out.count("duration mismatch") == 1
    lines = [json.loads(l) for l in playback_bus.DIAG_LOG_PATH.read_text().splitlines()]
    assert [l["track_id"] for l in lines if l["kind"] == "duration_mismatch"] == [t.id]
