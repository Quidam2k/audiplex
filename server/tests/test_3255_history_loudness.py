"""#3255: /api/playback/history gives start/end + loudness; loudness script + migration."""

import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from audiplex.database import _migrate_track_loudness
from audiplex.models import PlayStat, Track, User

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import measure_loudness  # noqa: E402


def _owner(db):
    return db.query(User).filter(User.username == "testuser").first()


def _track(db, album, title, path, lufs=None):
    t = Track(title=title, album_id=album.id, artist_id=album.artist_id,
              file_path=path, file_size=1, loudness_lufs=lufs)
    db.add(t)
    db.commit()
    return t


@pytest.fixture
def owner_is_testuser(monkeypatch):
    from audiplex.routers import playback

    s = playback.get_settings().model_copy(update={"dj_owner_username": "testuser"})
    monkeypatch.setattr(playback, "get_settings", lambda: s)


def _ago(now, minutes):
    return now - timedelta(minutes=minutes)


def test_history_pairs_start_with_its_own_end(client, db_session, sample_album, owner_is_testuser):
    owner = _owner(db_session)
    a = _track(db_session, sample_album, "A", "/x/a.mp3", lufs=-9.5)
    b = _track(db_session, sample_album, "B", "/x/b.mp3")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add_all([
        PlayStat(track_id=a.id, user_id=owner.id, event="start", timestamp=_ago(now, 30)),
        PlayStat(track_id=a.id, user_id=owner.id, event="complete", played_seconds=200, timestamp=_ago(now, 27)),
        PlayStat(track_id=b.id, user_id=owner.id, event="start", timestamp=_ago(now, 27)),
        PlayStat(track_id=b.id, user_id=owner.id, event="skip", timestamp=_ago(now, 26)),
        # A again, still playing: no end yet, and A's earlier complete must not be reused.
        PlayStat(track_id=a.id, user_id=owner.id, event="start", timestamp=_ago(now, 26)),
    ])
    db_session.commit()

    rows = client.get("/api/playback/history?limit=10").json()
    assert [(r["title"], r["end_event"]) for r in rows] == [("A", None), ("B", "skip"), ("A", "complete")]
    latest_a, b_row, first_a = rows
    assert latest_a["end"] is None
    assert first_a["id"] == first_a["track_id"] == a.id
    assert first_a["start"] == first_a["at"]
    assert first_a["end"] - first_a["start"] == pytest.approx(180, abs=1)
    assert first_a["loudness_lufs"] == -9.5
    assert b_row["loudness_lufs"] is None


def test_history_end_ignores_other_users_and_since_filters(client, db_session, sample_album, owner_is_testuser):
    owner = _owner(db_session)
    a = _track(db_session, sample_album, "A", "/x/a.mp3")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add_all([
        PlayStat(track_id=a.id, user_id=owner.id, event="start", timestamp=_ago(now, 60)),
        PlayStat(track_id=a.id, user_id=owner.id, event="start", timestamp=_ago(now, 10)),
        PlayStat(track_id=a.id, user_id=None, event="stop", timestamp=_ago(now, 9)),  # not the owner
        PlayStat(track_id=a.id, user_id=owner.id, event="stop", timestamp=_ago(now, 5)),
    ])
    db_session.commit()

    since = (datetime.now(timezone.utc) - timedelta(minutes=20)).timestamp()
    rows = client.get(f"/api/playback/history?since={since}").json()
    assert len(rows) == 1
    assert rows[0]["end_event"] == "stop"
    assert rows[0]["end"] - rows[0]["start"] == pytest.approx(300, abs=1)


def test_history_events_all_still_works(client, db_session, sample_album, owner_is_testuser):
    owner = _owner(db_session)
    a = _track(db_session, sample_album, "A", "/x/a.mp3")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add_all([
        PlayStat(track_id=a.id, user_id=owner.id, event="start", timestamp=_ago(now, 3)),
        PlayStat(track_id=a.id, user_id=owner.id, event="stop", timestamp=_ago(now, 2)),
    ])
    db_session.commit()
    rows = client.get("/api/playback/history?events=all").json()
    assert [r["event"] for r in rows] == ["stop", "start"]
    assert rows[0]["end"] is None  # only start rows get an end
    assert rows[1]["end_event"] == "stop"


def test_loudness_migration_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT)"))
    _migrate_track_loudness(engine)
    _migrate_track_loudness(engine)
    assert "loudness_lufs" in {c["name"] for c in inspect(engine).get_columns("tracks")}


def test_parse_integrated_reads_the_summary():
    stderr = (
        "[Parsed_ebur128_0 @ 0x1] t: 1.0 M: -20.0 S: -120.7 I: -20.0 LUFS\n"
        "[Parsed_ebur128_0 @ 0x1] Summary:\n\n"
        "  Integrated loudness:\n    I:         -14.2 LUFS\n    Threshold: -24.3 LUFS\n"
    )
    assert measure_loudness.parse_integrated(stderr) == -14.2
    assert measure_loudness.parse_integrated("  Integrated loudness:\n    I:         -inf LUFS\n") is None
    assert measure_loudness.parse_integrated("garbage") is None


def _make_db(path: Path, file_path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, file_path TEXT, loudness_lufs FLOAT);"
        "CREATE TABLE play_stats (id INTEGER PRIMARY KEY, track_id INT);"
    )
    con.executemany("INSERT INTO tracks VALUES (?,?,?,?)", [
        (1, "played", file_path, None),
        (2, "never played", file_path, None),
        (3, "already measured", file_path, -8.0),
    ])
    con.executemany("INSERT INTO play_stats (track_id) VALUES (?)", [(1,), (3,)])
    con.commit()
    con.close()


def test_apply_refuses_without_backup(tmp_path):
    db = tmp_path / "a.db"
    _make_db(db, "/nope.mp3")
    assert measure_loudness.main(["--db", str(db), "--apply"]) == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_measures_played_tracks_only_and_writes_with_backup(tmp_path):
    tone = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-af", "volume=-10dB", str(tone)],
        check=True,
    )
    db = tmp_path / "a.db"
    _make_db(db, str(tone))
    backup = tmp_path / "a.db.bak"
    shutil.copy(db, backup)

    rc = measure_loudness.main([
        "--db", str(db), "--apply", "--backup-done-at", str(backup),
        "--report", str(tmp_path / "r.json"),
    ])
    assert rc == 0
    rows = dict(sqlite3.connect(db).execute("SELECT id, loudness_lufs FROM tracks").fetchall())
    assert rows[2] is None  # never played: skipped by default
    assert rows[3] == -8.0  # already measured: untouched
    # lavfi's sine is 1/8 amplitude (-18 dBFS peak); -10 dB more is -28 dBFS peak,
    # and a mono sine reads ~3.8 dB lower in LUFS (RMS + K-weighting).
    assert rows[1] == pytest.approx(-31.8, abs=1.0)
