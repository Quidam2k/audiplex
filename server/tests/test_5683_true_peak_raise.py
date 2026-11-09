import math
import sqlite3
import sys
from pathlib import Path

import pytest

from audiplex import gain, loudness_live

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import loudness_backfill  # noqa: E402

SUMMARY = """[Parsed_ebur128_0 @ 000001] Summary:

  Integrated loudness:
    I:         -24.1 LUFS
    Threshold: -34.6 LUFS

  Loudness range:
    LRA:        12.3 LU

  True peak:
    Peak:       -1.7 dBFS
"""


def test_peaky_track_gets_budget_only():  # #5683
    assert gain.music_gain_db(-24, -14, max_boost_db=12, true_peak=-0.5) == 6.0
    g = gain.music_gain_db(-24, -14, max_boost_db=12, true_peak=-0.5, limiter_budget_db=0)
    assert g == 0.0


def test_quiet_flat_track_full_boost_and_config_dropback():  # #5683
    assert gain.music_gain_db(-26, -14, max_boost_db=12, true_peak=-12) == 12.0
    assert gain.music_gain_db(-26, -14, max_boost_db=6, true_peak=-12) == 6.0


def test_unknown_peak_keeps_old_behaviour():  # #5683
    assert gain.music_gain_db(-26, -14, max_boost_db=12, true_peak=None) == 6.0


def test_loud_track_still_cut():  # #5683
    assert gain.music_gain_db(-8, -14, max_boost_db=12, true_peak=-0.1) == -6.0


def test_book_gain_raise_only_with_peak():  # #5683
    assert gain.book_gain_db(-26, -14, 12, true_peak=-12) == 12.0
    assert gain.book_gain_db(-26, -14, 12, true_peak=None) == 6.0
    assert gain.book_gain_db(-10, -14, 12, true_peak=-12) == 0.0


def test_album_reference():  # #5683
    expected = round(10 * math.log10((10 ** -2 + 10 ** -1) / 2), 2)
    ref = gain.album_reference([(-20, -3.0, 100, 1), (-10, -2.0, 100, 2)])
    assert ref == (expected, -2.0)
    assert gain.album_reference([(-20, -3.0, 100, 0), (-10, -2.0, 100, 2)]) is None
    assert gain.album_reference([(-20, -3.0, 100, 1)]) is None
    assert gain.album_reference([(None, -3.0, 100, 1), (-10, -2.0, 100, 2)]) is None
    ref = gain.album_reference([(-20, None, 100, 1), (-10, -2.0, 100, 2)])
    assert ref == (expected, None)


def test_parse_summary():  # #5683
    assert loudness_live.parse_summary(SUMMARY) == (-24.1, -1.7)
    no_peak = SUMMARY.split("  True peak:")[0]
    assert loudness_live.parse_summary(no_peak) == (-24.1, None)
    inf_peak = SUMMARY.replace("-1.7 dBFS", "-inf dBFS")
    assert loudness_live.parse_summary(inf_peak) == (-24.1, None)
    assert loudness_live.parse_summary("") == (None, None)


def test_gate_ok():  # #5683
    assert loudness_backfill.gate_ok(16.0, 900, False) is True
    assert not loudness_backfill.gate_ok(8.0, 900, False)
    assert not loudness_backfill.gate_ok(16.0, 60, False)
    assert not loudness_backfill.gate_ok(16.0, 900, True)


def _db():
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE books(id INTEGER PRIMARY KEY, file_path TEXT, category TEXT,
                           loudness_lufs REAL, true_peak_dbtp REAL);
        CREATE TABLE tracks(id INTEGER PRIMARY KEY, file_path TEXT, content_kind TEXT,
                            loudness_lufs REAL, true_peak_dbtp REAL);
        CREATE TABLE play_stats(track_id INTEGER);
        """
    )
    return con


def _noop(*args, **kwargs):
    return None


def test_backfill_run_stores_and_counts(tmp_path):  # #5683
    f1, f2, f3 = (tmp_path / n for n in ("a.m4b", "t.mp3", "bad.m4b"))
    for f in (f1, f2, f3):
        f.write_bytes(b"x")
    missing = tmp_path / "gone.m4b"
    con = _db()
    con.execute("INSERT INTO books VALUES (1, ?, 'fiction', -16.0, NULL)", (str(f1),))
    con.execute("INSERT INTO books VALUES (2, ?, 'fiction', NULL, NULL)", (str(missing),))
    con.execute("INSERT INTO books VALUES (3, ?, 'fiction', NULL, NULL)", (str(f3),))
    con.execute("INSERT INTO tracks VALUES (1, ?, 'music', NULL, NULL)", (str(f2),))
    con.commit()

    def measure(path):
        if path == str(f3):
            raise RuntimeError("ffmpeg failed")
        return (-20.0, -3.0)

    todo = [
        ("book", 1, str(f1)),
        ("book", 2, str(missing)),
        ("track", 1, str(f2)),
        ("book", 3, str(f3)),
    ]
    result = loudness_backfill.run(
        con, todo, measure=measure, gate=lambda: True,
        sleep=_noop, emit=_noop, poll_s=5,
    )
    assert result["measured"] == 2
    assert result["failed"] == 1
    assert result["missing"] == 1
    assert con.execute(
        "SELECT loudness_lufs, true_peak_dbtp FROM books WHERE id=1").fetchone() == (-16.0, -3.0)
    assert con.execute(
        "SELECT loudness_lufs, true_peak_dbtp FROM tracks WHERE id=1").fetchone() == (-20.0, -3.0)
    assert con.execute(
        "SELECT loudness_lufs, true_peak_dbtp FROM books WHERE id=3").fetchone() == (None, None)


def test_backfill_gate_waits_without_skipping(tmp_path):  # #5683
    f = tmp_path / "t.mp3"
    f.write_bytes(b"x")
    con = _db()
    con.execute("INSERT INTO tracks VALUES (1, ?, 'music', NULL, NULL)", (str(f),))
    con.commit()
    answers = iter([False, False, True])
    sleeps = []
    result = loudness_backfill.run(
        con, [("track", 1, str(f))], measure=lambda p: (-22.0, -4.0),
        gate=lambda: next(answers, True), sleep=sleeps.append, emit=_noop, poll_s=5,
    )
    assert sleeps == [5, 5]
    assert result["measured"] == 1
    assert con.execute(
        "SELECT loudness_lufs, true_peak_dbtp FROM tracks WHERE id=1").fetchone() == (-22.0, -4.0)


def test_select_todo_order(tmp_path):  # #5683
    con = _db()
    p = lambda name: str(tmp_path / name)  # noqa: E731
    books = [
        (1, p("b1"), "meditation", -20.0, None),  # meditation, peak null
        (2, p("b2"), "fiction", None, None),  # other book, lufs null
        (3, p("b3"), "meditation", -18.0, -5.0),  # complete, skipped
        (4, p("b4"), "fiction", -18.0, None),  # not meditation, lufs set
    ]
    tracks = [
        (10, p("t10"), "music", None, None),  # null lufs
        (11, p("t11"), "music", -25.0, None),  # quiet, played once
        (12, p("t12"), "music", -25.0, None),  # quiet, played thrice
        (13, p("t13"), "music", -25.0, -10.0),  # quiet but has peak
        (14, p("t14"), "music", -15.0, None),  # not quiet
    ]
    con.executemany("INSERT INTO books VALUES (?,?,?,?,?)", books)
    con.executemany("INSERT INTO tracks VALUES (?,?,?,?,?)", tracks)
    con.executemany("INSERT INTO play_stats VALUES (?)", [(12,)] * 3 + [(11,)])
    con.commit()

    todo = loudness_backfill.select_todo(con, -14.0, 100)
    assert [(kind, ident) for kind, ident, _ in todo] == [
        ("book", 1), ("book", 2), ("track", 10), ("track", 12), ("track", 11),
    ]


def test_endpoint_album_mode_and_big_raise_log(client, db_session, sample_track, monkeypatch, tmp_path):  # #5683
    from audiplex.config import get_settings
    from audiplex.models import Track
    from audiplex.routers import playback
    s = get_settings()
    monkeypatch.setattr(s, "music_target_lufs", -14.0)
    monkeypatch.setattr(s, "music_max_boost_db", 12.0)
    monkeypatch.setattr(playback, "BIG_RAISE_LOG", tmp_path / "big.jsonl")
    monkeypatch.setattr(playback, "_big_raise_seen", set())
    sample_track.loudness_lufs, sample_track.true_peak_dbtp, sample_track.track_number = -26.0, -12.0, 1
    loud = Track(title="Loud", album_id=sample_track.album_id, artist_id=sample_track.artist_id,
                 track_number=2, duration_seconds=sample_track.duration_seconds or 100.0,
                 file_path=str(tmp_path / "loud.m4a"), content_kind="music",
                 loudness_lufs=-10.0, true_peak_dbtp=-0.5)
    db_session.add(loud)
    db_session.commit()
    track = client.get(f"/api/playback/gain/track/{sample_track.id}").json()
    assert (track["gain_db"], track["ref"]) == (12.0, "track")  # quiet and flat: the full raise
    album = client.get(f"/api/playback/gain/track/{sample_track.id}?album=true").json()
    assert album["ref"] == "album" and album["gain_db"] < 0  # the album is loud overall: kept in place
    lines = (tmp_path / "big.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and '"raise_db": 12.0' in lines[0] and '"headroom_db": 10.5' in lines[0]
    client.get(f"/api/playback/gain/track/{sample_track.id}")
    assert len((tmp_path / "big.jsonl").read_text(encoding="utf-8").splitlines()) == 1  # once per item
