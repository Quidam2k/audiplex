"""#1002: measured tempo + key, and sets ordered so each hand-off is compatible.

Detection is checked on synthetic audio with a known answer (click tracks at a
known BPM, chord progressions in a known key). The orderer is pure; the server
leaves out unanalysed/banned/non-music tracks and counts them; dj_harmonic_set
hands the ordered list to dj_mix unshuffled, replacing what's after the current song.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

from audiplex.harmonic import bpm_score, key_score, order_harmonic
from audiplex.models import Track
from audiplex.tempo_key import SR, analyze, camelot, key_name

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server" / "scripts"))
from audiplex_mcp import dj_toolkit, server as mcp  # noqa: E402
import measure_tempo_key as batch  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ----- detection on synthetic audio ----------------------------------------

def _clicks(bpm, secs=40.0, first=0.1):
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.01, int(SR * secs))
    n = int(0.03 * SR)
    hit = np.exp(-np.arange(n) / (0.005 * SR)) * np.sin(2 * np.pi * 1000 * np.arange(n) / SR)
    t = first
    while t < secs - 0.05:
        i = int(t * SR)
        x[i:i + n] += hit
        t += 60 / bpm
    return x


def _chords(chords, secs=3.0):
    t = np.arange(int(SR * secs)) / SR

    def note(m):
        f = 440 * 2 ** ((m - 69) / 12)
        return sum(np.sin(2 * np.pi * f * h * t) / h for h in (1, 2, 3))

    return np.concatenate([sum(note(m) for m in c) for c in chords * 3])


@pytest.mark.parametrize("bpm", [90, 128, 174])
def test_tempo_found_within_half_a_bpm_with_the_beat_phase(bpm):
    r = analyze(_clicks(bpm))
    assert abs(r["bpm"] - bpm) <= 0.5 and r["bpm_conf"] > 0.5
    assert abs(r["beat_offset"] - 0.1) < 0.03


def test_beat_offset_is_measured_from_the_track_start():
    beat = 60 / 120
    r = analyze(_clicks(120, first=0.1), start_s=30.2)  # window began 30.2 s in
    assert abs(r["beat_offset"] - (30.3 % beat)) < 0.03


@pytest.mark.parametrize("chords,code", [
    ([[60, 64, 67], [65, 69, 72], [67, 71, 74], [60, 64, 67]], "8B"),   # C major: I IV V I
    ([[57, 60, 64], [62, 65, 69], [64, 68, 71], [57, 60, 64]], "8A"),   # A minor
    ([[54, 57, 61], [59, 62, 66], [61, 65, 68], [54, 57, 61]], "11A"),  # F# minor
])
def test_key_found(chords, code):
    r = analyze(_chords(chords))
    assert r["musical_key"] == code and r["key_conf"] > 0.6


def test_silence_has_no_tempo_or_key():
    r = analyze(np.zeros(SR * 20))
    assert r["bpm"] is None and r["musical_key"] is None


def test_camelot_wheel():
    assert camelot(0, False) == "8B" and camelot(9, True) == "8A"   # C / Am
    assert camelot(7, False) == "9B" and camelot(4, True) == "9A"   # G / Em
    assert camelot(5, False) == "7B" and camelot(11, False) == "1B"  # F / B
    assert key_name("11A") == "F# minor" and key_name("zz") == "zz"
    codes = {camelot(pc, m) for pc in range(12) for m in (False, True)}
    assert len(codes) == 24


# ----- pure ordering ------------------------------------------------------

def test_key_and_bpm_scores():
    assert key_score("8A", "8A") == 1 and key_score("8A", "8B") == 0.9
    assert key_score("12A", "1A") == 0.9 and key_score("1B", "12B") == 0.9
    assert key_score("8B", "10B") == 0.6 and key_score("8B", "3B") == 0 and key_score("8B", "9A") == 0
    assert key_score(None, "8A") == 0
    assert bpm_score(128, 128) == 1 and bpm_score(70, 140) == pytest.approx(1)
    assert 0 < bpm_score(128, 132) < 1 and bpm_score(128, 150) == 0
    assert bpm_score(128, 150, tolerance=0.2) > 0


def test_chain_follows_compatible_keys_and_leaves_out_unanalysed():
    tracks = [(1, 128, "8A", 200, None), (2, 127, "3B", 200, None), (3, 129, "9A", 200, None),
              (4, 128, "10A", 200, None), (5, None, "8A", 200, None), (6, 128, "?", 200, None)]
    r = order_harmonic(tracks, start_id=1, seed=1)
    assert r["ordered"][:3] == [1, 3, 4] and set(r["ordered"]) == {1, 2, 3, 4}
    assert r["smooth"] == 2 and r["transitions"][-1]["key"] == 0


def test_minutes_start_and_arc():
    tracks = [(i, 120 + i, f"{i % 12 + 1}A", 240, e) for i, e in enumerate([90, 10, 50, 70, 30, 60])]
    assert len(order_harmonic(tracks, minutes=8, seed=2)["ordered"]) == 2
    rise = order_harmonic(tracks, arc="rise", seed=2)["ordered"]
    assert rise[0] == 1  # the lowest energy opens a rise
    down = order_harmonic(tracks, arc="wind_down", seed=2)["ordered"]
    assert down[0] == 0
    with pytest.raises(ValueError, match="rise"):
        order_harmonic(tracks, arc="bananas")
    assert order_harmonic([], seed=1) == {"ordered": [], "transitions": [], "smooth": 0}


def test_same_seed_same_set():
    tracks = [(i, 100 + i % 7, f"{i % 12 + 1}{'AB'[i % 2]}", 200, None) for i in range(30)]
    assert order_harmonic(tracks, seed=5) == order_harmonic(tracks, seed=5)


# ----- batch ---------------------------------------------------------------

def test_window_is_the_middle_two_minutes():
    assert batch.window(600) == (240.0, 120.0)
    assert batch.window(90) == (0.0, 90)
    assert batch.window(0) == (0.0, 120.0)


def test_batch_run_stops_when_busy_and_keeps_what_it_measured(tmp_path):
    files = []
    for i in range(6):
        f = tmp_path / f"{i}.m4a"
        f.write_bytes(b"x")
        files.append((i, f"T{i}", str(f), 200.0))
    files.insert(2, (9, "gone", str(tmp_path / "missing.m4a"), 200.0))
    result = {"bpm": 120.0, "bpm_conf": 0.9, "beat_offset": 0.1, "musical_key": "8A",
              "key_conf": 0.8, "key_margin": 0.1}
    calls = iter(["", "a DJ set is active"])
    rep = batch.run(files, "ffmpeg", sleep_s=0, recheck_every=2, busy=lambda: next(calls),
                    measurer=lambda ff, p, d: (result, "") if not p.endswith("1.m4a") else (None, "silent"))
    # re-checks after 2 files (idle) and after 4 (busy): 4 and 5 are never touched
    assert [m["id"] for m in rep["measured"]] == [0, 2, 3] and rep["failed"] == [{"id": 1, "reason": "silent"}]
    assert rep["aborted"] == "a DJ set is active" and rep["missing"] == [9]


def _db(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, file_path TEXT, duration_seconds REAL,
          content_kind TEXT DEFAULT 'music', bpm REAL, bpm_conf REAL, beat_offset REAL,
          musical_key TEXT, key_conf REAL);
        CREATE TABLE play_stats (track_id INTEGER);
        INSERT INTO tracks (id, title, file_path, duration_seconds, content_kind, bpm) VALUES
          (1, 'a', 'a', 200, 'music', NULL), (2, 'b', 'b', 200, 'music', 120),
          (3, 'c', 'c', 200, 'podcast', NULL), (4, 'd', 'd', 200, 'music', NULL);
        INSERT INTO play_stats VALUES (4), (4), (1);
    """)
    con.commit()
    return path, con


def test_select_rows_music_unanalysed_most_played_first(tmp_path):
    _, con = _db(tmp_path)
    assert [r[0] for r in batch.select_rows(con, True, [], 0)] == [4, 1]
    assert [r[0] for r in batch.select_rows(con, False, [], 1)] == [1]


def test_main_refuses_apply_without_backup_and_refuses_when_busy(tmp_path, monkeypatch, capsys):
    path, con = _db(tmp_path)
    con.close()
    assert batch.main(["--db", str(path), "--apply"]) == 2
    monkeypatch.setattr(batch, "busy_reason", lambda url, tok, bike: "a bike ride is active")
    assert batch.main(["--db", str(path)]) == 3
    assert "bike ride" in capsys.readouterr().err


def test_main_applies_only_to_empty_rows(tmp_path, monkeypatch):
    path, con = _db(tmp_path)
    con.close()
    bak = tmp_path / "bak"
    bak.write_bytes(b"x")
    monkeypatch.setattr(batch, "busy_reason", lambda url, tok, bike: "")
    monkeypatch.setattr(batch.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(batch, "measure", lambda ff, p, d: ({"bpm": 99.0, "bpm_conf": 0.5, "beat_offset": 0.2,
                                                            "musical_key": "5A", "key_conf": 0.7,
                                                            "key_margin": 0.1}, ""))
    # run() binds measure as a default at import; route through the patched one.
    real_run = batch.run
    monkeypatch.setattr(batch, "run", lambda rows, ff, **kw: real_run(rows, ff, measurer=batch.measure, **kw))
    assert batch.main(["--db", str(path), "--all-tracks", "--apply", "--backup-done-at", str(bak),
                       "--sleep", "0", "--report", str(tmp_path / "r.json")]) == 0
    con = sqlite3.connect(path)
    rows = dict(con.execute("SELECT id, musical_key FROM tracks").fetchall())
    assert rows == {1: "5A", 2: None, 3: None, 4: "5A"}  # 2 had a bpm, 3 is a podcast


def test_columns_migrate_on_an_old_db(tmp_path):
    from sqlalchemy import create_engine, inspect

    from audiplex.database import _migrate_track_tempo_key

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, bpm FLOAT)")
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{path}")
    _migrate_track_tempo_key(eng)
    _migrate_track_tempo_key(eng)  # idempotent, and a partial set is completed
    cols = {c["name"] for c in inspect(eng).get_columns("tracks")}
    assert {"bpm", "bpm_conf", "beat_offset", "musical_key", "key_conf"} <= cols


# ----- server --------------------------------------------------------------

def _track(db, album, title, bpm=None, key=None, seconds=200):
    t = Track(title=title, album_id=album.id, artist_id=album.artist_id, bpm=bpm, musical_key=key,
              file_path=f"/m/{title}.mp3", file_size=1, duration_seconds=seconds)
    db.add(t)
    db.commit()
    return t


def test_order_endpoint_orders_and_counts_what_it_left_out(client, db_session, sample_album):
    a = _track(db_session, sample_album, "a", 128, "8A")
    b = _track(db_session, sample_album, "b", 128, "9A")
    c = _track(db_session, sample_album, "c", 127, "3B")
    raw = _track(db_session, sample_album, "raw")
    banned = _track(db_session, sample_album, "banned", 128, "8B")
    client.post("/api/playback/bans", json={"track_ids": [banned.id]})
    ids = [a.id, b.id, c.id, raw.id, banned.id]
    r = client.post("/api/playback/harmonic/order", json={"track_ids": ids, "start_track_id": a.id, "seed": 1}).json()
    assert r["ordered"] == [a.id, b.id, c.id] and r["keys"] == ["8A", "9A", "3B"]
    assert r["bpms"] == [128, 128, 127] and r["smooth"] == 1
    assert (r["unanalysed"], r["dropped"]) == (1, 1) and r["minutes"] == 10.0
    assert client.post("/api/playback/harmonic/order", json={"track_ids": ids, "arc": "x"}).status_code == 400


# ----- MCP -----------------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    d = {"posts": [], "mix": None}

    async def fake_post(path, body):
        d["posts"].append((path, body))
        return {"ordered": [3, 1, 2], "keys": ["8A", "9A", "9B"], "bpms": [128.0, 127.2, 129.0],
                "transitions": [{"key": 0.9, "bpm": 0.8}, {"key": 0.9, "bpm": 0.7}], "smooth": 2,
                "minutes": 12.0, "unanalysed": 4, "dropped": 0}

    async def fake_resolve(kind, query, recursive=True):
        if query == "none":
            raise LookupError("No tracks tagged 'none'.")
        return f"{kind} '{query}'", [{"id": 1}, {"id": 2}, {"id": 3}]

    async def fake_recent(ids, hours):
        return ids, 0

    async def fake_mix(**kw):
        d["mix"] = kw
        return "MIXED"

    monkeypatch.setattr(mcp, "_post", fake_post)
    monkeypatch.setattr(mcp, "_resolve_source", fake_resolve)
    monkeypatch.setattr(mcp, "_recent_split", fake_recent)
    monkeypatch.setattr(mcp, "dj_mix", fake_mix)
    return d


def test_harmonic_set_orders_then_replaces_the_tail_unshuffled(api):
    out = run(dj_toolkit.dj_harmonic_set(sources=[{"kind": "folder", "query": "/m"}], minutes=12,
                                         start_track_id=3, bpm_tolerance=0.04, arc="rise"))
    path, body = api["posts"][-1]
    assert path == "/api/playback/harmonic/order" and body["track_ids"] == [1, 2, 3]
    assert (body["start_track_id"], body["bpm_tolerance"], body["arc"], body["minutes"]) == (3, 0.04, "rise", 12)
    assert api["mix"] == {"track_ids": [3, 1, 2], "shuffle": False, "keep_upcoming": False,
                          "exclude_recent_hours": 0}
    assert "2 of 2 hand-offs" in out and "8A 128 > 9A 127 > 9B 129" in out
    assert "4 without a measured tempo/key yet" in out and out.endswith("MIXED")


def test_harmonic_set_refuses_an_empty_source_and_sends_nothing(api):
    out = run(dj_toolkit.dj_harmonic_set(sources=[{"kind": "tag", "query": "none"}]))
    assert out.startswith("REFUSED") and api["posts"] == [] and api["mix"] is None
    assert run(dj_toolkit.dj_harmonic_set()) == "Give sources or track_ids for the set."
