"""#4222: sampled book loudness - pure helpers of scripts/measure_book_loudness.py."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import measure_book_loudness as mbl  # noqa: E402


def test_window_starts_spread_over_the_book():
    assert mbl.window_starts(7000, 6, 60) == [970.0, 1970.0, 2970.0, 3970.0, 4970.0, 5970.0]
    assert mbl.window_starts(0, 6, 60) == [0.0]  # unknown duration: one capped pass
    assert mbl.window_starts(300, 6, 60) == [0.0]  # too short for six windows


def test_book_lufs_is_a_median_needing_half_the_windows():
    assert mbl.book_lufs([-14.3, -15.2, -14.4, -15.6, -16.1, -15.8]) == -15.4
    assert mbl.book_lufs([-18.0, None, -17.0, None]) == -17.5
    assert mbl.book_lufs([-18.0, None, None, None]) is None


def test_lane_rate_ignores_a_run_reset():
    assert mbl.lane_rate((0.0, 100.0), (10.0, 200.0)) == 10.0
    assert mbl.lane_rate((0.0, 900.0), (10.0, 5.0)) is None
    assert mbl.lane_rate((5.0, 1.0), (5.0, 2.0)) is None


def test_apply_adds_the_column_and_never_overwrites(tmp_path, monkeypatch):
    db, backup, book = tmp_path / "a.db", tmp_path / "a.bak", tmp_path / "b.m4b"
    backup.write_bytes(b"x")
    book.write_bytes(b"x")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, file_path TEXT, "
                "duration_seconds REAL)")
    con.executemany("INSERT INTO books VALUES (?, ?, ?, 7000)",
                    [(1, "One", str(book)), (2, "Gone", str(tmp_path / "nope.m4b"))])
    con.commit()
    con.close()
    monkeypatch.setattr(mbl, "measure", lambda ffmpeg, path, start, window: (-18.0, ""))
    args = ["--db", str(db), "--no-guard", "--report", str(tmp_path / "r.json")]
    assert mbl.main(args + ["--apply"]) == 2  # no backup named
    assert mbl.main(args + ["--apply", "--backup-done-at", str(backup)]) == 0
    con = sqlite3.connect(db)
    assert con.execute("SELECT id, loudness_lufs FROM books ORDER BY id").fetchall() == [
        (1, -18.0), (2, None)]
    con.execute("UPDATE books SET loudness_lufs=-30 WHERE id=1")
    con.commit()
    con.close()
    mbl.main(args + ["--apply", "--backup-done-at", str(backup), "--ids", "1"])
    assert sqlite3.connect(db).execute("SELECT loudness_lufs FROM books WHERE id=1").fetchone() == (-30.0,)


def test_lane_guard_judges_only_new_status_writes(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    status = tmp_path / "status.json"
    writes = iter([(100, 0), (400, 3000), (700, 4000), (1000, 7000)])

    def write():
        at, audio = next(writes)
        status.write_text(json.dumps({"lanes": {"A": {
            "state": "running", "updated_at": at, "audio_s_done_run": audio}}}))
    write()
    monkeypatch.setattr(mbl.time, "sleep", lambda s: write())
    a = SimpleNamespace(no_guard=False, lane_status=str(status), lane="A",
                        baseline_s=900, min_ratio=0.7, max_pause_s=3600)
    g = mbl.LaneGuard(a)  # (100,0) -> (400,3000): 10 audio s/s
    assert g.baseline == 10.0
    assert g.check() is False and g.paused_s == 0  # same write as before: no verdict
    write()  # (700,4000): 3.3 s/s, slow -> pauses until (1000,7000): 10 s/s
    assert g.check() is False
    assert g.prev == (1000.0, 7000.0)
