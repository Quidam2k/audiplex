"""Regression tests for loudness profiles, issue #7335."""

import asyncio
import json
import shutil
import sqlite3
import sys
import wave
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import measure_profile as mp  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as srv  # noqa: E402


def make_frames(segments, integrated=-14.2):
    """Build synthetic #7335 frames with a constant final integrated value."""
    frames = []
    for duration_s, momentary_db in segments:
        for _ in range(round(duration_s / mp.FRAME_S)):
            frames.append({
                "t": len(frames) * mp.FRAME_S,
                "m": float(momentary_db), "s": float(momentary_db),
                "i": integrated,
            })
    return frames


def _connection():
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, "
        "file_path TEXT, content_kind TEXT, loudness_lufs REAL)"
    )
    return con


@pytest.fixture
def memory_db():
    with closing(_connection()) as con:
        yield con


def test_profile_detects_intro_outro_and_mid_dip():
    """#7335: detect a quiet intro, terminal fade, and one interior dip."""
    segments = [(15.0, -40), (15.0, -14), (4.0, -26), (6.0, -14)]
    segments.extend((mp.FRAME_S, db) for db in np.linspace(-14, -60, 100))
    profile = mp.profile_from_frames(make_frames(segments))
    assert profile["integrated_lufs"] == pytest.approx(-14.2, abs=0.05)
    assert profile["intro_quiet_s"] == pytest.approx(15.0, abs=1.0)
    assert profile["outro_fade_s"] == pytest.approx(8.3, abs=1.5)
    assert profile["duration_s"] == pytest.approx(50.0, abs=0.2)
    assert len(profile["dips"]) == 1
    dip = profile["dips"][0]
    assert dip["start_s"] == pytest.approx(30.0, abs=1.0)
    assert dip["end_s"] == pytest.approx(34.0, abs=1.0)
    assert dip["depth_db"] == pytest.approx(12.0, abs=2.5)
    assert dip["kind"] == "mid"


def test_warmup_sentinels_are_ignored():
    """#7335: startup sentinels must not create an intro or starting dip."""
    frames = make_frames([(0.2, -120.7), (0.2, -70.0), (20.0, -14)])
    profile = mp.profile_from_frames(frames)
    assert 0.0 <= profile["intro_quiet_s"] <= 0.5
    assert profile["dips"] == []


def test_no_loud_section_gives_none():
    """#7335: a uniformly quiet track has no loud boundaries or dips."""
    profile = mp.profile_from_frames(make_frames([(20.0, -60)], integrated=-14.0))
    assert profile["intro_quiet_s"] is None
    assert profile["outro_fade_s"] is None
    assert profile["dips"] == []


def test_empty_and_no_integrated():
    """#7335: empty input and missing integrated values are safe."""
    for frames in ([], make_frames([(20.0, -14)], integrated=None)):
        profile = mp.profile_from_frames(frames)
        assert profile["integrated_lufs"] is None
        assert profile["dips"] == []


def test_parse_ebur_output_handles_inf_and_missing():
    """#7335: parse timestamps and finite values while retaining missing data."""
    text = """frame:0    pts:0       pts_time:0
lavfi.r128.M=-inf
lavfi.r128.S=-inf
lavfi.r128.I=-70.0
frame:1    pts:1600    pts_time:0.1
lavfi.r128.M=-23.5
lavfi.r128.S=-24.0
lavfi.r128.I=-23.9
"""
    assert mp.parse_ebur_output(text) == [
        {"t": 0.0, "m": None, "s": None, "i": -70.0},
        {"t": 0.1, "m": -23.5, "s": -24.0, "i": -23.9},
    ]
    assert mp.parse_ebur_output("frame:0    pts:0       pts_time:0") == [
        {"t": 0.0, "m": None, "s": None, "i": None},
    ]


def test_analyze_real_wav_with_ffmpeg(tmp_path):
    """#7335: analyze a generated WAV without playback or external services."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not available")
    rate = 44100
    t = np.arange(30 * rate, dtype=float) / rate
    amplitudes = np.repeat([0.003, 0.3, 0.3 * 10 ** (-30 / 20)], 10 * rate)
    samples = np.rint(amplitudes * np.sin(2 * np.pi * 440 * t) * 32767)
    path = tmp_path / "profile.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.astype("<i2").tobytes())
    result = mp.analyze_file(str(path))
    assert isinstance(result["integrated_lufs"], float)
    assert -40 < result["integrated_lufs"] < -5
    assert 8.0 <= result["intro_quiet_s"] <= 12.0
    assert result["analyzer_version"] == mp.ANALYZER_VERSION
    assert 29.5 <= result["duration_s"] <= 30.5
    assert result["path"] == str(path)


def test_store_is_idempotent_and_mirrors_loudness(memory_db):
    """#7335: repeated storage replaces dips and preserves measured loudness."""
    con = memory_db
    con.execute("INSERT INTO tracks VALUES (7, 'Song', '/x.mp3', 'music', NULL)")
    mp.ensure_tables(con)
    mp.ensure_tables(con)
    profile = {
        "integrated_lufs": -15.5, "intro_quiet_s": 3.0,
        "outro_fade_s": 4.0, "duration_s": 200.0,
        "analyzer_version": mp.ANALYZER_VERSION,
        "dips": [{"start_s": 50.0, "end_s": 53.0, "depth_db": 9.0, "kind": "mid"}],
    }
    mp.store(con, 7, profile)
    mp.store(con, 7, profile)
    assert con.execute(
        "SELECT integrated_lufs FROM track_audio_profile WHERE track_id=7"
    ).fetchall() == [(-15.5,)]
    assert con.execute(
        "SELECT COUNT(*) FROM track_dips WHERE track_id=7"
    ).fetchone() == (1,)
    assert con.execute("SELECT loudness_lufs FROM tracks WHERE id=7").fetchone() == (-15.5,)
    mp.store(con, 7, {**profile, "integrated_lufs": None})
    assert con.execute("SELECT loudness_lufs FROM tracks WHERE id=7").fetchone() == (-15.5,)


def test_select_rows_played_first_and_skips_analyzed(memory_db):
    """#7335: prioritize plays, exclude books, and skip the current analyzer."""
    con = memory_db
    con.execute("CREATE TABLE play_stats (id INTEGER PRIMARY KEY, track_id INTEGER)")
    con.executemany("INSERT INTO tracks VALUES (?, ?, ?, ?, NULL)", [
        (i, f"Track {i}", f"/{i}.mp3", "book" if i == 4 else "music")
        for i in range(1, 6)
    ])
    con.executemany("INSERT INTO play_stats (track_id) VALUES (?)", [
        (i,) for i, count in ((1, 0), (2, 3), (3, 1), (4, 5), (5, 2))
        for _ in range(count)
    ])
    assert con.execute(
        "SELECT name FROM sqlite_master WHERE name='track_audio_profile'"
    ).fetchone() is None
    assert [row[0] for row in mp.select_rows(con, [], 0)] == [2, 5, 3, 1]
    con.execute(
        "INSERT INTO track_audio_profile (track_id, analyzer_version) VALUES (?, ?)",
        (5, mp.ANALYZER_VERSION),
    )
    assert [row[0] for row in mp.select_rows(con, [], 0)] == [2, 3, 1]
    assert [row[0] for row in mp.select_rows(con, [1, 4], 0)] == [1]
    assert [row[0] for row in mp.select_rows(con, [], 1)] == [2]


def test_talk_busy_states(tmp_path, monkeypatch):
    """#7335: distinguish active Talk, idle, missing, and unreadable state."""
    monkeypatch.setattr(mp.time, "sleep", lambda _: None)
    path = tmp_path / "speech_state.json"
    assert mp.talk_busy(str(path)) == ""
    path.write_text(json.dumps({"talk_active": True, "other": 1}), encoding="ascii")
    assert "Talk" in mp.talk_busy(str(path))
    path.write_text(json.dumps({"talk_active": False}), encoding="ascii")
    assert mp.talk_busy(str(path)) == ""
    path.write_text("not json", encoding="ascii")
    assert "can't read" in mp.talk_busy(str(path))


def test_run_batch_waits_while_busy_and_never_crashes(tmp_path, monkeypatch):
    """#7335: recheck busy per file, tolerate failures, and honor dry runs."""
    first, missing, third = (tmp_path / name for name in ("a.mp3", "missing.mp3", "c.mp3"))
    first.write_bytes(b"")
    third.write_bytes(b"")
    rows = [(1, "A", str(first)), (2, "B", str(missing)), (3, "C", str(third))]

    def analyze(p, ffmpeg="ffmpeg"):
        if p == str(third):
            raise RuntimeError("boom")
        return {
            "integrated_lufs": -14.0, "intro_quiet_s": 1.0, "outro_fade_s": 2.0,
            "duration_s": 10.0, "dips": [], "analyzer_version": mp.ANALYZER_VERSION,
            "path": p,
        }

    monkeypatch.setattr(mp, "analyze_file", analyze)
    for dry_run in (False, True):
        messages, sleeps, busy_calls = [], [], []
        states = iter(["music is playing", "music is playing", "", "", ""])

        def busy():
            busy_calls.append(1)
            return next(states, "")

        with closing(_connection()) as con:
            mp.ensure_tables(con)
            con.executemany("INSERT INTO tracks VALUES (?, ?, ?, 'music', NULL)", [
                (1, "A", str(first)), (3, "C", str(third)),
            ])
            summary = mp.run_batch(
                rows, con=con, ffmpeg="ffmpeg", dry_run=dry_run, sleep_ratio=0,
                poll_s=30.0, busy=busy, log=messages.append,
                sleeper=sleeps.append, now=lambda: 0.0,
            )
            assert {key: summary[key] for key in (
                "analyzed", "skipped_missing", "failed", "checked"
            )} == {"analyzed": 1, "skipped_missing": 1, "failed": 1, "checked": 3}
            assert any(line.startswith("waiting: music is playing") for line in messages)
            assert sleeps == [30.0, 30.0]
            assert len(busy_calls) == 5
            assert con.execute("SELECT track_id FROM track_audio_profile").fetchall() == (
                [] if dry_run else [(1,)]
            )


def test_endpoint_track_profile(client, db_session, sample_track):
    """#7335: existing authenticated test fixtures expose measured profiles."""
    from audiplex.models import TrackAudioProfile, TrackDip

    track_id = sample_track.id
    url = f"/api/playback/track-profile/{track_id}"
    response = client.get(url)
    assert response.status_code == 200
    assert response.json() == {
        "track_id": track_id, "measured": False, "profile": None, "dips": [],
    }
    assert client.get("/api/playback/track-profile/999999").status_code == 404
    db_session.add(TrackAudioProfile(
        track_id=track_id, integrated_lufs=-14.0, intro_quiet_s=15.0,
        outro_fade_s=8.0, duration_s=50.0,
        analyzed_at="2026-10-08T00:00:00+00:00", analyzer_version="7335-1",
    ))
    db_session.add(TrackDip(
        track_id=track_id, start_s=30.0, end_s=34.0, depth_db=12.0, kind="mid",
    ))
    db_session.commit()
    response = client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["measured"] is True
    assert data["profile"]["intro_quiet_s"] == 15.0
    assert len(data["dips"]) == 1
    assert data["dips"][0]["kind"] == "mid"


def test_mcp_profile_line(monkeypatch):
    """#7335: format measured, unmeasured, and unavailable profiles offline."""
    data = {
        "track_id": 5, "measured": True,
        "profile": {
            "integrated_lufs": -14.0, "intro_quiet_s": 18.2, "outro_fade_s": 9.1,
            "duration_s": 240.0, "analyzed_at": "x", "analyzer_version": "7335-1",
        },
        "dips": [{"start_s": 130.0, "end_s": 144.0, "depth_db": 11.2, "kind": "mid"}],
    }

    async def fake_get(path):
        assert path == "/api/playback/track-profile/5"
        return data

    monkeypatch.setattr(srv, "_get", fake_get)
    assert asyncio.run(srv._profile_line(5)) == (
        "intro quiet 18s \u00b7 fade 9s \u00b7 dips 2:10-2:24 (-11 dB, mid)"
    )
    data = {"track_id": 5, "measured": False, "profile": None, "dips": []}
    assert asyncio.run(srv._profile_line(5)) == "loudness profile: not measured yet"

    async def failing_get(path):
        raise RuntimeError("boom")

    monkeypatch.setattr(srv, "_get", failing_get)
    assert asyncio.run(srv._profile_line(5)) == "loudness profile: unavailable"


def test_track_audio_profile_migration_is_idempotent():
    """#7335: the migration creates both tables once and is safe to run again."""
    from sqlalchemy import create_engine, inspect, text

    from audiplex.database import _migrate_track_audio_profile

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tracks (id INTEGER PRIMARY KEY)"))
    _migrate_track_audio_profile(engine)
    _migrate_track_audio_profile(engine)
    names = set(inspect(engine).get_table_names())
    assert {"track_audio_profile", "track_dips"} <= names
    columns = {c["name"] for c in inspect(engine).get_columns("track_dips")}
    assert {"track_id", "start_s", "end_s", "depth_db", "kind"} <= columns
