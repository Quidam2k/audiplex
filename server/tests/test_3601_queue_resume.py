"""#3601: a DJ-built queue survives the phone app being killed.

The queue lives only in phone RAM; the server keeps the last real one each
renderer reported (atomically, on disk) and dj_resume puts it back PAUSED.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from audiplex import playback_bus
from audiplex.playback_bus import bus, queue_snapshot

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import dj_toolkit, server as mcp  # noqa: E402


@pytest.fixture(autouse=True)
def reset_bus():
    bus.reset()
    yield


def _state(playing=True, qi=1, track=12, pos=61000, origin="dj"):
    return {
        "playing": playing,
        "track": {"id": track, "title": "T"},
        "position_ms": pos,
        "queue_index": qi,
        "queue_length": 4,
        "queue": [
            {"index": 0, "id": 11, "title": "A", "artist": "X"},
            {"index": 1, "id": 12, "title": "B", "artist": "Y"},
            {"index": 2, "id": -5, "title": "DJ clip"},
            {"index": 3, "id": 13, "title": "C", "artist": "Z"},
        ],
        "queue_origin": origin,
    }


def test_snapshot_keeps_real_tracks_and_spot():
    snap = queue_snapshot(_state())
    assert snap["track_ids"] == [11, 12, 13]
    assert (snap["index"], snap["track_id"], snap["position_ms"]) == (1, 12, 61000)
    assert (snap["title"], snap["artist"], snap["origin"]) == ("B", "Y", "dj")


def test_snapshot_on_a_clip_resumes_next_track_from_start():
    snap = queue_snapshot(_state(qi=2, track=-5))
    assert (snap["index"], snap["track_id"], snap["position_ms"]) == (2, 13, 0)


def test_nothing_replayable_is_none():
    assert queue_snapshot({"queue": [], "track": None}) is None
    assert queue_snapshot({"queue": [{"index": 0, "id": -1}], "track": {"id": -1}}) is None


def test_state_report_persists_and_empty_report_never_clobbers(client):
    client.post("/api/playback/state?device_id=phone", json=_state(playing=False))
    saved = json.loads(playback_bus.LAST_QUEUE_PATH.read_text())["phone"]
    assert saved["track_ids"] == [11, 12, 13] and saved["origin"] == "dj"
    # A relaunched app reports an empty player: the snapshot must survive it.
    client.post("/api/playback/state?device_id=phone", json={"playing": False})
    resp = client.get("/api/playback/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["track_id"] == 12 and body["position_ms"] == 61000 and body["age_seconds"] >= 0
    assert not playback_bus.LAST_QUEUE_PATH.with_name("last_queue.json.tmp").exists()


def test_heartbeats_dont_rewrite_until_refresh(client, monkeypatch):
    writes = []
    monkeypatch.setattr(playback_bus, "_save_last_queue", lambda d, s: writes.append(s))
    for pos in (1000, 2000, 3000):
        client.post("/api/playback/state", json=_state(pos=pos))
    assert len(writes) == 1
    client.post("/api/playback/state", json=_state(playing=False, pos=4000))  # pause = change
    assert len(writes) == 2 and writes[-1]["position_ms"] == 4000


def test_resume_404_when_nothing_saved(client):
    assert client.get("/api/playback/resume").status_code == 404


# ----- dj_resume ----------------------------------------------------------

SNAP = {"track_ids": list(range(100, 160)), "index": 2, "track_id": 102, "title": "B",
        "artist": "Y", "position_ms": 61000, "age_seconds": 300}


def _wire(monkeypatch, ack_status="ok"):
    sent = []

    async def fake_get(path):
        assert path == "/api/playback/resume"
        return SNAP

    async def fake_enqueue(cmd, payload):
        sent.append((cmd, payload))
        return {"id": len(sent)}

    async def fake_ack(cid, timeout):
        return {"ack_status": ack_status}

    monkeypatch.setattr(mcp, "_get", fake_get)
    monkeypatch.setattr(mcp, "_enqueue", fake_enqueue)
    monkeypatch.setattr(mcp, "_await_ack", fake_ack)
    return sent


def test_dj_resume_restores_paused_from_the_saved_spot(monkeypatch):
    sent = _wire(monkeypatch)
    out = asyncio.run(dj_toolkit.dj_resume())
    assert sent[0] == ("activate", {"track_ids": list(range(102, 142)), "position_ms": 61000,
                                    "playing": False})
    assert sent[1] == ("queue", {"track_ids": list(range(142, 160))})
    assert "paused" in out and "Y - B at 1:01" in out and "58 track(s)" in out


def test_dj_resume_on_old_phone_never_falls_back_to_playing(monkeypatch):
    sent = _wire(monkeypatch, ack_status="unknown_type")
    out = asyncio.run(dj_toolkit.dj_resume())
    assert [c for c, _ in sent] == ["activate"]
    assert "1.0.49" in out and "play=True" in out


def test_dj_resume_play_true_starts_then_seeks(monkeypatch):
    sent = _wire(monkeypatch)
    asyncio.run(dj_toolkit.dj_resume(play=True))
    assert sent == [("play_now", {"track_ids": list(range(102, 160))}),
                    ("seek", {"position_ms": 61000})]
