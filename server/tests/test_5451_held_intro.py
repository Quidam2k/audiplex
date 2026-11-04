"""#5451: a HELD intro is not an announcement.

2026-11-03 15:47: Jarvis said "Bike mode on, here come the tunes", both clips went
muted_hold (the audio seat sat on a muted desk), and the #3249 gate still passed
because it only read chat TEXT. The music cut into Todd's YouTube unannounced.
Now: an intro whose every clip is held (or still queued onto a seat Pantheon says
cannot play) is refused with a recoverable line (dj_announce on the music path),
and a dj_announce from this process counts as the announcement.
"""

import asyncio
import datetime
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp  # noqa: E402


def _iso(seconds_ago):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _db(tmp_path, clips, events=(), seat=None):
    """clips: [(speech_id, status)] for ONE persona intro message; events: [(speech_id, type)]."""
    p = tmp_path / "pantheon.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, sender TEXT, content TEXT, timestamp TEXT)")
    con.execute("CREATE TABLE speech_queue (id INTEGER PRIMARY KEY, message_id INTEGER, "
                "parent_message_id INTEGER, status TEXT)")
    con.execute("CREATE TABLE audio_events (id INTEGER PRIMARY KEY, speech_id INTEGER, event_type TEXT)")
    con.execute("INSERT INTO messages (id, sender, content, timestamp) VALUES (7, 'claude', "
                "'Bike mode on, here come the tunes.', ?)", (_iso(10),))
    con.executemany("INSERT INTO speech_queue (id, message_id, status) VALUES (?, 7, ?)", clips)
    con.executemany("INSERT INTO audio_events (speech_id, event_type) VALUES (?, ?)", events)
    con.commit()
    con.close()
    if seat is not None:
        (tmp_path / "runtime").mkdir(exist_ok=True)
        (tmp_path / "runtime" / "playback_state.json").write_text(json.dumps(
            {"eligible": seat, "written_at_monotonic": time.time()}), encoding="utf-8")
    return p


@pytest.fixture
def wire(monkeypatch):
    sent = []

    async def fake_get(path):
        return {"playing": False, "queue": []}

    async def fake_raw(cmd_type, payload):
        sent.append(cmd_type)
        return {"id": len(sent), "pending": 1}

    monkeypatch.setattr(mcp, "_get", fake_get)
    monkeypatch.setattr(mcp, "_enqueue_raw", fake_raw)
    monkeypatch.setattr(mcp, "_last_dj_announce", {"at": 0.0}, raising=False)
    return sent


def _start():
    return asyncio.run(mcp._enqueue("resume", {}))


def test_held_only_intro_is_refused_recoverably(wire, tmp_path, monkeypatch):
    monkeypatch.setenv("DJ_PANTHEON_DB", str(_db(tmp_path, [(1, "muted"), (2, "muted")])))
    out = _start()
    assert isinstance(out, str) and out.startswith("REFUSED (announce first): your intro was HELD")
    assert "dj_announce(" in out and "mode='now'" in out  # the same-turn recovery
    assert mcp._held_kind(out) == "announce_first"
    assert wire == []


def test_played_intro_passes(wire, tmp_path, monkeypatch):
    monkeypatch.setenv("DJ_PANTHEON_DB", str(_db(tmp_path, [(1, "played")], [(1, "play_start")])))
    assert isinstance(_start(), dict) and wire == ["resume"]


def test_pending_intro_passes_when_seat_can_play(wire, tmp_path, monkeypatch):
    # Todd 38728: the music comes up UNDER the intro, so a queued intro must not block.
    monkeypatch.setenv("DJ_PANTHEON_DB", str(_db(tmp_path, [(1, "pending")], seat=True)))
    assert isinstance(_start(), dict)


def test_pending_intro_onto_dead_seat_is_refused(wire, tmp_path, monkeypatch):
    monkeypatch.setenv("DJ_PANTHEON_DB", str(_db(tmp_path, [(1, "pending")], seat=False)))
    assert _start().startswith("REFUSED (announce first): your intro was HELD")


def test_stale_seat_state_fails_open(wire, tmp_path, monkeypatch):
    db = _db(tmp_path, [(1, "ready")], seat=False)
    st = tmp_path / "runtime" / "playback_state.json"
    st.write_text(json.dumps({"eligible": False, "written_at_monotonic": time.time() - 300}), encoding="utf-8")
    monkeypatch.setenv("DJ_PANTHEON_DB", str(db))
    assert isinstance(_start(), dict)


def test_one_clip_started_counts_as_heard(wire, tmp_path, monkeypatch):
    monkeypatch.setenv("DJ_PANTHEON_DB", str(_db(tmp_path, [(1, "muted"), (2, "playing")], [(2, "play_start")])))
    assert isinstance(_start(), dict)


def test_text_only_message_still_counts(wire, tmp_path, monkeypatch):
    monkeypatch.setenv("DJ_PANTHEON_DB", str(_db(tmp_path, [])))  # no speech rows: as before #5451
    assert isinstance(_start(), dict)


def test_dj_announce_counts_as_the_intro(wire, tmp_path, monkeypatch):
    monkeypatch.setenv("DJ_PANTHEON_DB", str(_db(tmp_path, [(1, "muted")])))
    assert _start().startswith("REFUSED")
    monkeypatch.setitem(mcp._last_dj_announce, "at", time.time() - 5)
    assert isinstance(_start(), dict)


def test_dj_announce_stamps_the_clock(wire, monkeypatch):
    async def fake_render(text, title, kind, artist):
        return {"clip_id": 3, "url": "/c/3", "duration_seconds": 2.0}

    monkeypatch.setattr(mcp, "_render_clip", fake_render)
    monkeypatch.setattr(mcp, "_mute_hold", lambda cmd: None)
    monkeypatch.setattr(mcp, "_someone_talking", lambda: False)
    monkeypatch.setattr(mcp, "_talk_hold", lambda cmd: None)
    out = asyncio.run(mcp.dj_announce("Alright, here comes the music.", mode="now"))
    assert out.startswith("Queued") and time.time() - mcp._last_dj_announce["at"] < 5


def test_heard_continuation_chunk_counts(wire, tmp_path, monkeypatch):  # Codex P2
    db = _db(tmp_path, [(1, "muted")])
    con = sqlite3.connect(db)
    con.execute("INSERT INTO speech_queue (id, message_id, parent_message_id, status) VALUES (2, NULL, 7, 'played')")
    con.execute("INSERT INTO audio_events (speech_id, event_type) VALUES (2, 'play_ack')")
    con.commit()
    con.close()
    monkeypatch.setenv("DJ_PANTHEON_DB", str(db))
    assert isinstance(_start(), dict)


def test_served_but_unstarted_onto_dead_seat_is_refused(wire, tmp_path, monkeypatch):  # Codex P2
    monkeypatch.setenv("DJ_PANTHEON_DB", str(_db(tmp_path, [(1, "playing")], seat=False)))
    assert _start().startswith("REFUSED (announce first): your intro was HELD")


def test_deferred_dj_announce_earns_no_credit(wire, monkeypatch):  # Codex P2
    async def fake_render(text, title, kind, artist):
        return {"clip_id": 3, "url": "/c/3", "duration_seconds": 2.0}

    monkeypatch.setattr(mcp, "_render_clip", fake_render)
    monkeypatch.setattr(mcp, "_mute_hold", lambda cmd: None)
    monkeypatch.setattr(mcp, "_someone_talking", lambda: False)
    monkeypatch.setattr(mcp, "_talk_hold", lambda cmd: None)
    assert asyncio.run(mcp.dj_announce("Here comes the music.", mode="next")).startswith("Queued")
    assert mcp._last_dj_announce["at"] == 0.0
