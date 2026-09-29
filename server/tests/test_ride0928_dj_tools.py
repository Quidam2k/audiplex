"""#ride0928: prev/now/next brief, pair notes in the brief, dj_folder, dj_set_kind, dj_history."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402

STATE = {
    "playing": True, "queue_index": 1, "track": {"id": 20, "title": "Now", "artist": "B"},
    "queue": [
        {"index": 0, "id": 10, "title": "Before", "artist": "A"},
        {"index": 1, "id": 20, "title": "Now", "artist": "B"},
        {"index": 2, "id": -7, "title": "DJ break", "artist": "DJ"},
        {"index": 3, "id": 30, "title": "After", "artist": "C"},
        {"index": 4, "id": 40, "title": "Later", "artist": "D"},
    ],
}


@pytest.fixture
def wire(monkeypatch):
    calls = []
    st = {"state": STATE, "notes": {}, "history": []}

    async def fake_get(path):
        calls.append(("GET", path))
        if path.startswith("/api/playback/state"):
            return st["state"]
        if path.startswith("/api/playback/pair-notes"):
            return st["notes"].get(path, [])
        if path.startswith("/api/playback/history"):
            return st["history"]
        if path.startswith("/api/music/folders/tracks"):
            return [{"id": 1}, {"id": 2}]
        return []

    async def fake_post(path, body):
        calls.append(("POST", path, body))
        if path == "/api/playback/tracks/playable":
            return {"playable": body["track_ids"], "missing": []}
        if path == "/api/playback/playlists":
            return {"id": 9, "name": body["name"], "track_count": len(body["track_ids"])}
        if path == "/api/playback/pair-notes":
            return {"id": 3, **body}
        return {}

    async def fake_put(path, body):
        calls.append(("PUT", path, body))
        return {"kind": body["kind"], "updated": 4}

    async def fake_raw(cmd, payload):
        calls.append(("CMD", cmd, payload))
        return {"id": 1, "pending": 1}

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_put", fake_put)
    monkeypatch.setattr(mcp_server, "_enqueue_raw", fake_raw)
    return calls, st


def test_brief_names_previous_now_next_and_pair_note(wire):
    _, st = wire
    st["notes"]["/api/playback/pair-notes?track_a=20&track_b=30&limit=5"] = [
        {"id": 1, "track_a": 20, "track_b": 30, "note": "great lift", "persona": "claude"}]
    out = asyncio.run(mcp_server.dj_break_brief())
    assert "Previous: Before - A" in out
    assert "Now playing: Now - B" in out
    assert "Next: After - C" in out  # the DJ break between is skipped
    assert "After that: Later - D" in out
    assert "DJ note on this pairing (claude): great lift" in out


def test_brief_previous_falls_back_to_history(wire):
    _, st = wire
    st["state"] = {**STATE, "queue_index": 0, "queue": [{"index": 0, "id": 20, "title": "Now", "artist": "B"}]}
    st["history"] = [{"track_id": 20, "title": "Now"}, {"track_id": 5, "title": "Earlier", "artist_name": "Z"}]
    out = asyncio.run(mcp_server.dj_break_brief())
    assert "Previous: Earlier - Z" in out and "Next:" not in out


def test_dj_folder_actions(wire):
    calls, _ = wire
    out = asyncio.run(mcp_server.dj_folder("Q:/Music/Ride", action="queue"))
    assert out.startswith("RESULT sent=2 held=no")
    assert ("CMD", "queue", {"track_ids": [1, 2]}) in calls
    out = asyncio.run(mcp_server.dj_folder("Q:/Music/Ride/", action="playlist"))
    assert "Saved playlist 'Ride' (#9) with 2 track(s)" in out
    assert "Unknown action" in asyncio.run(mcp_server.dj_folder("x", action="burn"))


def test_dj_folder_shuffle_goes_through_dj_mix(wire, monkeypatch):
    seen = {}

    async def fake_mix(**kw):
        seen.update(kw)
        return "mixed"

    monkeypatch.setattr(mcp_server, "dj_mix", fake_mix)
    assert asyncio.run(mcp_server.dj_folder("Q:/Music/Ride", recursive=False)) == "mixed"
    assert seen["sources"] == [{"kind": "folder", "query": "Q:/Music/Ride", "recursive": False}]


def test_pair_note_and_set_kind_and_history(wire):
    calls, st = wire
    assert "Saved note #3 on 20 -> 30" in asyncio.run(mcp_server.dj_pair_note(20, "lift", 30, "claude"))
    assert asyncio.run(mcp_server.dj_set_kind("podcast", folder="Q:/Pods")) == "Set 4 track(s) to podcast."
    assert ("PUT", "/api/playback/content-kind", {"kind": "podcast", "folder": "Q:/Pods"}) in calls
    st["history"] = [{"track_id": 5, "title": "Song", "artist_name": "Band", "at": 1_700_000_000}]
    assert "5 | Band - Song" in asyncio.run(mcp_server.dj_history())
