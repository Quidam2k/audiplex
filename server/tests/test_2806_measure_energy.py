"""#2806: scripts/measure_energy.py: scoring, busy guard, batch abort, --apply."""

import io
import json
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import measure_energy as me  # noqa: E402

SR = 11025


def _lively(seconds=4):
    rng = np.random.default_rng(1)
    x = rng.uniform(-1, 1, SR * seconds) * 0.02
    step = int(0.25 * SR)
    for s in range(0, len(x) - step, step):
        x[s:s + int(0.06 * SR)] = rng.uniform(-0.8, 0.8, int(0.06 * SR))
    return x


def test_quiet_sine_scores_below_loud_bursts():
    t = np.arange(SR * 4) / SR
    quiet = me.energy_score(me.features(0.05 * np.sin(2 * np.pi * 440 * t), SR))
    loud = me.energy_score(me.features(_lively(), SR))
    assert quiet < 25 and loud > 50 and loud > quiet + 40
    assert 0 <= quiet <= 100 and 0 <= loud <= 100


def test_silence_has_no_rms_and_scores_within_range():
    f = me.features(np.zeros(SR * 2), SR)
    assert f["rms_db"] is None
    assert 0 <= me.energy_score({"rms_db": 5.0, "onset_rate": 99, "zcr": 1.0}) <= 100
    assert me.energy_score({"rms_db": -90.0, "onset_rate": 0, "zcr": 0}) == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg")
def test_measure_real_wav_and_failures(tmp_path):
    wav = tmp_path / "s.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    str(wav)], check=True)
    e, f, reason = me.measure("ffmpeg", str(wav))
    assert reason == "" and isinstance(e, int) and 0 <= e <= 100 and f["seconds"] > 2.5
    assert me.measure("ffmpeg", str(tmp_path / "nope.wav"))[0] is None


class _Resp(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(json.dumps(body).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(monkeypatch, state=None, pool=None, pool_404=False, down=False):
    def fake(req, timeout=0):
        if down:
            raise urllib.error.URLError("refused")
        if req.full_url.endswith("/api/playback/state"):
            return _Resp(state if state is not None else {"playing": False})
        if pool_404:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        return _Resp(pool if pool is not None else {"active": False})
    monkeypatch.setattr(me.urllib.request, "urlopen", fake)


def _bike(tmp_path, active):
    p = tmp_path / "bike.json"
    p.write_text(json.dumps({"active": active}))
    return str(p)


def test_busy_reason_cases(monkeypatch, tmp_path):
    _fake_urlopen(monkeypatch)
    assert me.busy_reason("http://x", "t", _bike(tmp_path, False)) == ""
    assert me.busy_reason("http://x", "t", str(tmp_path / "missing.json")) == ""
    assert me.busy_reason("http://x", "t", _bike(tmp_path, True))
    _fake_urlopen(monkeypatch, state={"playing": True})
    assert me.busy_reason("http://x", "t", _bike(tmp_path, False))
    _fake_urlopen(monkeypatch, pool={"active": True})
    assert me.busy_reason("http://x", "t", _bike(tmp_path, False))
    _fake_urlopen(monkeypatch, pool_404=True)
    assert me.busy_reason("http://x", "t", _bike(tmp_path, False)) == ""
    _fake_urlopen(monkeypatch, down=True)
    assert "can't verify" in me.busy_reason("http://x", "t", _bike(tmp_path, False))


def _fake_measure(monkeypatch):
    monkeypatch.setattr(me, "measure", lambda ff, p: (
        42, {"rms_db": -20.0, "onset_rate": 1.0, "zcr": 0.05, "seconds": 3.0}, ""))


def _files(tmp_path, n):
    rows = []
    for i in range(1, n + 1):
        p = tmp_path / f"{i}.mp3"
        p.write_bytes(b"x")
        rows.append((i, f"T{i}", str(p)))
    return rows


def test_run_aborts_on_busy_recheck_and_keeps_measured(monkeypatch, tmp_path):
    _fake_measure(monkeypatch)
    sleeps, calls = [], []

    def busy():
        calls.append(1)
        return "music is playing" if len(calls) == 2 else ""
    rep = me.run(_files(tmp_path, 10), "ffmpeg", sleep_s=1.5, recheck_every=3,
                 busy=busy, sleeper=sleeps.append)
    assert len(rep["measured"]) == 6 and rep["aborted"] == "music is playing"
    assert sleeps and all(s == 1.5 for s in sleeps)


def _db(tmp_path, with_energy=True):
    p = tmp_path / "a.db"
    con = sqlite3.connect(p)
    energy = "energy INTEGER, " if with_energy else ""
    con.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, file_path TEXT, "
                + energy + "content_kind TEXT)")
    con.execute("CREATE TABLE play_stats (track_id INTEGER)")
    for tid, title, fp in _files(tmp_path, 3):
        con.execute("INSERT INTO tracks (id,title,file_path,content_kind) VALUES (?,?,?,?)",
                    (tid, title, fp, "music"))
    con.execute("INSERT INTO tracks (id,title,file_path,content_kind) VALUES (4,'Book',?,'audiobook')",
                (str(tmp_path / "1.mp3"),))
    if with_energy:
        con.execute("UPDATE tracks SET energy=7 WHERE id=3")
    con.executemany("INSERT INTO play_stats VALUES (?)", [(i,) for i in (1, 2, 3, 4)])
    con.commit()
    con.close()
    return str(p)


def test_main_apply_without_backup_exits_2(tmp_path):
    assert me.main(["--db", _db(tmp_path), "--apply", "--skip-busy-check"]) == 2


def test_main_missing_energy_column_exits_2(tmp_path):
    assert me.main(["--db", _db(tmp_path, with_energy=False), "--skip-busy-check"]) == 2


def test_main_apply_fills_only_null_music(monkeypatch, tmp_path):
    _fake_measure(monkeypatch)
    db, bak = _db(tmp_path), tmp_path / "bak"
    bak.write_bytes(b"b")
    rc = me.main(["--db", db, "--apply", "--backup-done-at", str(bak), "--skip-busy-check",
                  "--sleep", "0", "--report", str(tmp_path / "r.json")])
    assert rc == 0
    rows = dict(sqlite3.connect(db).execute("SELECT id, energy FROM tracks"))
    assert rows == {1: 42, 2: 42, 3: 7, 4: None}
    assert json.loads((tmp_path / "r.json").read_text())["applied"] is True


def test_main_busy_at_start_exits_3_and_writes_nothing(monkeypatch, tmp_path):
    _fake_measure(monkeypatch)
    _fake_urlopen(monkeypatch, state={"playing": True})
    db, bak = _db(tmp_path), tmp_path / "bak"
    bak.write_bytes(b"b")
    rc = me.main(["--db", db, "--apply", "--backup-done-at", str(bak),
                  "--bike-state", _bike(tmp_path, False), "--report", str(tmp_path / "r.json")])
    assert rc == 3
    n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM tracks WHERE energy=42").fetchone()[0]
    assert n == 0
    assert not (tmp_path / "r.json").exists()
