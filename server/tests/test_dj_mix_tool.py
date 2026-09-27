"""dj_mix MCP tool branches (#2842), with the HTTP layer stubbed out.

The planning itself is covered by test_mix.py and the /mix/plan endpoint test;
this pins what the tool SENDS: replace_upcoming after the current song, play_now
only when nothing is loaded, and an append-only fallback for old phone builds.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402

PLAYING = {
    "track": {"id": 5, "title": "Now"},
    "queue_index": 1,
    "queue": [{"index": 0, "id": 4}, {"index": 1, "id": 5}, {"index": 2, "id": 6}],
}


@pytest.fixture
def wire(monkeypatch):
    sent, posted = [], []
    calls = {"state": PLAYING, "ack": {"ack_status": "ok"}}

    async def fake_get(path):
        assert path == "/api/playback/state"
        return calls["state"]

    async def fake_post(path, body):
        posted.append(body)
        tail = body.get("upcoming_ids", []) + [i for i in body["new_ids"] if i not in body.get("upcoming_ids", [])]
        return {"upcoming": tail, "summary": "s"}

    async def fake_enqueue(cmd_type, payload):
        sent.append((cmd_type, payload["track_ids"]))
        return {"id": len(sent), "pending": 1}

    async def fake_ack(command_id, timeout=12.0):
        return calls["ack"]

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_enqueue", fake_enqueue)
    monkeypatch.setattr(mcp_server, "_await_ack", fake_ack)
    return sent, posted, calls


def run(**kw):
    return asyncio.run(mcp_server.dj_mix(**kw))


def test_playing_sends_replace_upcoming_and_spares_current(wire):
    sent, posted, _ = wire
    out = run(track_ids=[7, 8], shuffle=False)
    assert posted[0]["current_id"] == 5
    assert posted[0]["played_ids"] == [4]
    assert posted[0]["upcoming_ids"] == [6]
    assert sent == [("replace_upcoming", [6, 7, 8])]
    assert "after the current song" in out


def test_nothing_loaded_starts_with_play_now(wire):
    sent, posted, calls = wire
    calls["state"] = {"track": None, "queue": [], "queue_index": 0}
    run(track_ids=[7, 8])
    assert "current_id" not in posted[0]
    assert sent == [("play_now", [7, 8])]


def test_old_phone_build_falls_back_to_appending_only_new(wire):
    sent, _, calls = wire
    calls["ack"] = {"ack_status": "unknown_type", "ack_detail": "replace_upcoming"}
    out = run(track_ids=[6, 7])
    assert sent == [("replace_upcoming", [6, 7]), ("queue", [7])]
    assert "Old phone build" in out


def test_live_stream_is_left_alone(wire):
    sent, _, calls = wire
    calls["state"] = {"track": {"id": -1}, "queue": [], "queue_index": 0}
    out = run(track_ids=[7])
    assert sent == []
    assert "live stream" in out


def test_track_ids_file(wire, tmp_path):
    sent, _, calls = wire
    calls["state"] = {"track": None, "queue": [], "queue_index": 0}
    f = tmp_path / "ids.json"
    f.write_text("[9, 10]", encoding="utf-8")
    run(track_ids_file=str(f))
    assert sent == [("play_now", [9, 10])]


def test_voice_break_playing_is_not_interrupted(wire):
    """A DJ break (negative id) is the current item; a mix goes after it."""
    sent, posted, calls = wire
    calls["state"] = {
        "track": {"id": -3, "title": "DJ break"},
        "queue_index": 0,
        "queue": [{"index": 0, "id": -3}, {"index": 1, "id": 6}],
    }
    run(track_ids=[7], shuffle=False)
    assert posted[0]["current_id"] == -3
    assert sent[0][0] == "replace_upcoming"


def test_fully_trimmed_plan_still_clears_the_tail(wire, monkeypatch):
    """Everything still queued already played → the tail is emptied, not kept."""
    sent, _, _ = wire

    async def empty_plan(path, body):
        return {"upcoming": [], "summary": "s"}

    monkeypatch.setattr(mcp_server, "_post", empty_plan)
    run(track_ids=[4])
    assert sent == [("replace_upcoming", [])]
