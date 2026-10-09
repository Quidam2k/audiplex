"""#3981: dip textures stored, backfilled, migrated and served; norm_gain_db; --watch."""

import asyncio
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import measure_profile as mp  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as srv  # noqa: E402

OLD_DIPS = """CREATE TABLE track_dips (id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL, start_s REAL, end_s REAL, depth_db REAL, kind TEXT)"""


@pytest.fixture
def con(tmp_path):
    song = tmp_path / "song.m4a"
    song.write_bytes(b"x")
    with closing(sqlite3.connect(":memory:")) as c:
        c.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, "
                  "file_path TEXT, content_kind TEXT, loudness_lufs REAL)")
        c.execute("INSERT INTO tracks VALUES (7, 'Song', ?, 'music', NULL)", (str(song),))
        yield c


def test_ensure_tables_adds_texture_columns_to_a_7335_table(con):
    con.execute(OLD_DIPS)
    mp.ensure_tables(con)
    mp.ensure_tables(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(track_dips)")}
    assert {"texture", "texture_conf"} <= cols


def test_store_keeps_dip_texture(con):
    mp.ensure_tables(con)
    mp.store(con, 7, {
        "integrated_lufs": -15.0, "intro_quiet_s": 0.0, "outro_fade_s": 0.0,
        "duration_s": 100.0, "analyzer_version": mp.ANALYZER_VERSION,
        "dips": [{"start_s": 50.0, "end_s": 53.0, "depth_db": 9.0, "kind": "mid",
                  "texture": "vocal_likely", "texture_conf": 0.7}],
    })
    assert con.execute("SELECT texture, texture_conf FROM track_dips").fetchall() == [
        ("vocal_likely", 0.7)]


def test_backfill_labels_only_unlabeled_dips_and_waits_while_busy(con, monkeypatch):
    con.execute(OLD_DIPS)
    con.executemany("INSERT INTO track_dips (track_id, start_s, end_s, depth_db, kind) "
                    "VALUES (7, ?, ?, 9, 'mid')", [(10.0, 13.0), (40.0, 44.0)])
    mp.ensure_tables(con)
    con.execute("UPDATE track_dips SET texture='silence', texture_conf=1 WHERE start_s=40")
    monkeypatch.setattr(mp, "texture_of", lambda path, dip, ffmpeg: ("rhythm_only", 0.6))
    states = iter(["a DJ set is active", ""])
    slept, logs = [], []
    summary = mp.backfill_textures(con, ffmpeg="ffmpeg", poll_s=5, busy=lambda: next(states),
                                   log=logs.append, sleeper=slept.append)
    assert summary["dips"] == 1 and summary["labeled"] == 1
    assert slept == [5] and logs == ["waiting: a DJ set is active"]
    assert con.execute("SELECT start_s, texture FROM track_dips ORDER BY start_s").fetchall() == [
        (10.0, "rhythm_only"), (40.0, "silence")]


def test_texture_of_fails_soft(monkeypatch):
    def boom(*a, **k):
        raise ValueError("bad file")
    monkeypatch.setattr(mp.dip_texture, "decode_window", boom)
    assert mp.texture_of("x", {"start_s": 1, "end_s": 3}, "ffmpeg") == (None, None)


def test_run_batch_labels_new_dips(con, monkeypatch):
    mp.ensure_tables(con)
    path = con.execute("SELECT file_path FROM tracks").fetchone()[0]
    monkeypatch.setattr(mp, "analyze_file", lambda p, f: {
        "integrated_lufs": -15.0, "intro_quiet_s": 0.0, "outro_fade_s": 0.0,
        "duration_s": 100.0, "analyzer_version": mp.ANALYZER_VERSION,
        "dips": [{"start_s": 5.0, "end_s": 8.0, "depth_db": 9.0, "kind": "intro"}]})
    monkeypatch.setattr(mp, "texture_of", lambda p, d, f: ("ambient", 0.9))
    mp.run_batch([(7, "Song", path)], con=con, ffmpeg="ffmpeg", dry_run=False,
                 sleep_ratio=0, poll_s=1, busy=lambda: "", log=lambda m: None)
    assert con.execute("SELECT texture FROM track_dips").fetchall() == [("ambient",)]


def test_watch_loops_until_stopped(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    with closing(sqlite3.connect(db)) as c:
        c.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, "
                  "file_path TEXT, content_kind TEXT, loudness_lufs REAL)")
        c.execute("CREATE TABLE play_stats (id INTEGER PRIMARY KEY, track_id INTEGER)")
        c.commit()
    calls = []
    monkeypatch.setattr(mp, "run_batch", lambda rows, **k: calls.append("batch") or {})
    monkeypatch.setattr(mp, "backfill_textures", lambda con, **k: calls.append("tex") or {})

    class Stop(Exception):
        pass

    def fake_sleep(s):
        calls.append(s)
        if calls.count(30.0) == 2:
            raise Stop
    monkeypatch.setattr(mp.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        mp.main(["--db", str(db), "--log", str(tmp_path / "l.log"), "--watch",
                 "--watch-s", "30", "--skip-busy-check"])
    assert calls == ["batch", "tex", 30.0, "batch", "tex", 30.0]


def test_migration_adds_texture_columns_to_existing_table():
    from sqlalchemy import create_engine, inspect, text

    from audiplex.database import _migrate_track_audio_profile

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tracks (id INTEGER PRIMARY KEY)"))
        conn.execute(text(OLD_DIPS))
    _migrate_track_audio_profile(engine)
    _migrate_track_audio_profile(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("track_dips")}
    assert {"texture", "texture_conf"} <= columns


def test_endpoint_serves_texture_and_attenuate_only_gain(client, db_session, sample_track):
    from audiplex.config import get_settings
    from audiplex.models import TrackAudioProfile, TrackDip

    target = get_settings().music_target_lufs
    db_session.add(TrackAudioProfile(track_id=sample_track.id, integrated_lufs=target + 6.5,
                                     analyzer_version="7335-1"))
    db_session.add(TrackDip(track_id=sample_track.id, start_s=1.0, end_s=4.0, depth_db=9.0,
                            kind="intro", texture="ambient", texture_conf=0.8))
    db_session.commit()
    data = client.get(f"/api/playback/track-profile/{sample_track.id}").json()
    assert data["profile"]["norm_gain_db"] == -6.5
    assert data["dips"][0]["texture"] == "ambient"
    assert data["dips"][0]["texture_conf"] == 0.8
    row = db_session.get(TrackAudioProfile, sample_track.id)
    row.integrated_lufs = target - 5.0  # a quiet track is never boosted
    db_session.commit()
    data = client.get(f"/api/playback/track-profile/{sample_track.id}").json()
    assert data["profile"]["norm_gain_db"] == 0.0


def test_mcp_profile_line_names_the_texture_as_a_guess(monkeypatch):
    async def fake_get(path):
        return {"measured": True, "profile": {},
                "dips": [{"start_s": 130.0, "end_s": 144.0, "depth_db": 11.2,
                          "kind": "mid", "texture": "sparse_instrumental"}]}
    monkeypatch.setattr(srv, "_get", fake_get)
    assert asyncio.run(srv._profile_line(5)) == (
        "dips 2:10-2:24 (-11 dB, mid, sounds like sparse instrumental?)")
