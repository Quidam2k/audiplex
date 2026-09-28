"""#3249: persona music control must actually start music, and announce first.

2026-09-28 13:17: play_now [1450] ACKED but the file sat on a drive letter that
no longer existed, the stream 404'd, and Todd heard nothing. This pins:
  - /api/playback/tracks/playable splits ids by whether the file is on disk
  - the MCP never sends a dead file (drops it, or refuses if nothing is left)
  - Todd's announce-first rule: an idle player won't start without a persona
    having mentioned the music in chat recently; the refusal says what to say
  - recent phone player_errors are surfaced instead of "it acked"
"""

import asyncio
import datetime
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402
from audiplex.models import Track  # noqa: E402


# ---- server endpoint -------------------------------------------------------

def test_playable_splits_on_disk_vs_missing(client, db_session, sample_album, tmp_path):
    real = tmp_path / "here.m4a"
    real.write_bytes(b"x")
    here = Track(title="here", album_id=sample_album.id, artist_id=sample_album.artist_id,
                 file_path=str(real), file_size=1)
    gone = Track(title="gone", album_id=sample_album.id, artist_id=sample_album.artist_id,
                 file_path="E:\\no\\such\\drive.m4a", file_size=1)
    db_session.add_all([here, gone])
    db_session.commit()
    r = client.post("/api/playback/tracks/playable",
                    json={"track_ids": [gone.id, here.id, -2, 999999]})
    assert r.status_code == 200
    assert r.json() == {"playable": [here.id, -2], "missing": [gone.id, 999999]}


# ---- MCP gate + filter -----------------------------------------------------

def _chat_db(tmp_path, rows):
    p = tmp_path / "pantheon.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, sender TEXT, content TEXT, timestamp TEXT)")
    con.executemany("INSERT INTO messages (sender, content, timestamp) VALUES (?,?,?)", rows)
    con.commit()
    con.close()
    return p


def _iso(seconds_ago):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


@pytest.fixture
def wire(monkeypatch, tmp_path):
    sent = []
    st = {"state": {"playing": False, "queue": []}, "missing": [], "log": []}

    async def fake_get(path):
        if path.startswith("/api/playback/client-log"):
            return st["log"]
        return st["state"]

    async def fake_post(path, body):
        assert path == "/api/playback/tracks/playable"
        ids = body["track_ids"]
        return {"playable": [i for i in ids if i not in st["missing"]],
                "missing": [i for i in ids if i in st["missing"]]}

    async def fake_raw(cmd_type, payload):
        sent.append((cmd_type, payload))
        return {"id": len(sent), "pending": 1}

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_enqueue_raw", fake_raw)
    monkeypatch.setenv("DJ_PANTHEON_DB", str(tmp_path / "absent.db"))
    return sent, st


def _enq(cmd, payload):
    return asyncio.run(mcp_server._enqueue(cmd, payload))


def test_idle_player_unannounced_is_refused(wire, tmp_path, monkeypatch):
    sent, _ = wire
    db = _chat_db(tmp_path, [("user", "play some music", _iso(20)),
                             ("claude", "Sure thing", _iso(15))])
    monkeypatch.setenv("DJ_PANTHEON_DB", str(db))
    out = _enq("play_now", {"track_ids": [1]})
    assert out.startswith("REFUSED (announce first)")
    assert "say()" in out and "Pause your book" in out
    assert sent == []


def test_announced_start_goes_through(wire, tmp_path, monkeypatch):
    sent, _ = wire
    db = _chat_db(tmp_path, [("claude", "Starting the music in ten seconds, Boss.", _iso(12))])
    monkeypatch.setenv("DJ_PANTHEON_DB", str(db))
    assert isinstance(_enq("resume", {}), dict)
    assert sent == [("resume", {})]


def test_old_announcement_does_not_count(wire, tmp_path, monkeypatch):
    sent, _ = wire
    db = _chat_db(tmp_path, [("claude", "Here come the tunes", _iso(mcp_server.ANNOUNCE_WINDOW_S + 60))])
    monkeypatch.setenv("DJ_PANTHEON_DB", str(db))
    assert _enq("play_now", {"track_ids": [1]}).startswith("REFUSED")


def test_already_playing_and_non_start_commands_skip_gate(wire, tmp_path, monkeypatch):
    sent, st = wire
    monkeypatch.setenv("DJ_PANTHEON_DB", str(_chat_db(tmp_path, [])))
    assert isinstance(_enq("pause", {}), dict)
    st["state"] = {"playing": False, "queue": [{"index": 0, "id": 9}]}
    assert isinstance(_enq("queue", {"track_ids": [2]}), dict)  # paused queue: append only
    st["state"] = {"playing": True, "queue": [{"index": 0, "id": 9}]}
    assert isinstance(_enq("play_now", {"track_ids": [3]}), dict)
    assert [c for c, _ in sent] == ["pause", "queue", "play_now"]


def test_unreadable_chat_fails_open(wire):
    sent, _ = wire  # DJ_PANTHEON_DB points at a file that doesn't exist
    assert isinstance(_enq("play_now", {"track_ids": [1]}), dict)


def test_dead_files_are_dropped_or_refused(wire):
    sent, st = wire
    st["state"] = {"playing": True}
    st["missing"] = [1450]
    data = _enq("play_now", {"track_ids": [1450, 16]})
    assert sent[-1] == ("play_now", {"track_ids": [16]})
    assert data["dropped_missing"] == [1450]
    assert "isn't on disk" in mcp_server._missing_note(data)
    st["missing"] = [1450, 16]
    out = _enq("play_now", {"track_ids": [1450, 16]})
    assert isinstance(out, str) and "nothing was sent" in out
    assert len(sent) == 1


def test_player_errors_are_surfaced(wire):
    _, st = wire
    st["log"] = [
        {"event": "player_error", "at": time.time() - 120, "message": "Source error",
         "detail": {"trackId": "1450", "trackTitle": "Paint The Town Blue",
                    "causeMessage": "Response code: 404"}},
        {"event": "player_error", "at": time.time() - 99999, "detail": {"trackId": "1"}},
        {"event": "play_when_ready", "at": time.time()},
    ]
    lines = asyncio.run(mcp_server._player_error_lines())
    assert len(lines) == 1
    assert "PHONE COULD NOT PLAY track 1450" in lines[0] and "404" in lines[0]


def test_big_lists_go_out_in_small_commands(wire):
    # 13:17 and 13:37: a ~990-track queue froze the phone's command loop ~3 min.
    sent, st = wire
    st["state"] = {"playing": True}
    data = _enq("queue", {"track_ids": list(range(1, 101))})
    assert [(c, len(p["track_ids"])) for c, p in sent] == [("queue", 40), ("queue", 40), ("queue", 20)]
    assert [i for _, p in sent for i in p["track_ids"]] == list(range(1, 101))
    assert data["sent_count"] == 100 and data["chunks"] == 3
    sent.clear()
    _enq("play_now", {"track_ids": list(range(1, 51))})
    assert [c for c, _ in sent] == ["play_now", "queue"]


def test_mix_skips_replace_upcoming_once_phone_said_unknown(monkeypatch, tmp_path):
    sent = []
    state = {"playing": True, "track": {"id": 5}, "queue_index": 0,
             "queue": [{"index": 0, "id": 5}, {"index": 1, "id": 6}]}
    history = [{"type": "replace_upcoming", "ack_status": "unknown_type"}]

    async def fake_get(path):
        return history if path.startswith("/api/playback/commands") else state

    async def fake_post(path, body):
        if path == "/api/playback/tracks/playable":
            return {"playable": body["track_ids"], "missing": []}
        return {"upcoming": [6, 7, 8], "summary": "s"}

    async def fake_raw(cmd_type, payload):
        sent.append(cmd_type)
        return {"id": len(sent), "pending": 1}

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_enqueue_raw", fake_raw)
    monkeypatch.setenv("DJ_MIX_SOURCES_FILE", str(tmp_path / "sources.json"))  # #5463
    monkeypatch.setitem(mcp_server._SWAP, "task", None)
    out = asyncio.run(mcp_server.dj_mix(track_ids=[7, 8], shuffle=False, exclude_recent_hours=0))
    # #5463: no re-sent replace_upcoming AND no append (appending segregated the
    # sources); the whole mix is swapped in at the song boundary instead.
    assert sent == [], sent
    assert "when the current song ends" in out
