"""#2843: "the DJ says your phone took the play command, but no music plays."

A start tool may only say playing=YES when the device acked ok AND then
reported playing what was asked. A failed ack is NO; an old phone build that
acks on receipt but stays silent is never YES (Karen rider b); a late honest
ack is waited for (the old 8 s wait raced the phone's own 8 s start timeout).
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp  # noqa: E402


@pytest.fixture
def dev(monkeypatch):
    """A scripted device: `ack` / `state` are callables of elapsed seconds."""
    t0 = time.time()
    d = {"ack": lambda s: None, "state": lambda s: None, "sent": [], "gets": 0}

    async def fake_raw(cmd_type, payload):
        d["sent"].append((cmd_type, payload))
        return {"id": 77, "type": cmd_type, "pending": 1}

    async def fake_get(path):
        el = time.time() - t0
        if path.startswith("/api/playback/commands"):
            a = d["ack"](el)
            row = {"id": 77, "created_at": t0}
            return [dict(row, **a)] if a else [row]
        if path.startswith("/api/playback/state"):
            d["gets"] += 1
            st = d["state"](el)
            return dict(st, updated_at=t0 + el) if st else {"playing": False, "updated_at": None}
        if path.startswith("/api/playback/device"):
            return {"connected": True}
        return []

    async def fake_post(path, body):
        if path == "/api/playback/tracks/playable":
            return {"playable": body["track_ids"], "missing": []}
        return {}

    async def no_gate(cmd_type):
        return None

    monkeypatch.setattr(mcp, "_enqueue_raw", fake_raw)
    monkeypatch.setattr(mcp, "_get", fake_get)
    monkeypatch.setattr(mcp, "_post", fake_post)
    monkeypatch.setattr(mcp, "_announce_gate", no_gate)
    monkeypatch.setattr(mcp, "START_VERIFY_S", 1.0)
    monkeypatch.setattr(mcp, "VERIFY_POLL_S", 0.02)
    return d


def playing(track_id, title="Song"):
    return lambda s: {"playing": True, "track": {"id": track_id, "title": title}}


def first_line(out):
    return out.split("\n", 1)[0]


def test_ack_ok_and_state_playing_is_yes(dev):
    dev["ack"] = lambda s: {"ack_status": "ok"}
    dev["state"] = playing(5)
    out = asyncio.run(mcp.dj_play_now([5, 6]))
    assert "playing=YES" in first_line(out)
    assert "PLAYING=" not in out and "should pick this up" in out


def test_failed_ack_is_no_and_returns_early(dev):
    dev["ack"] = lambda s: {"ack_status": "failed", "ack_detail": "not playing 8s after load"}
    started = time.time()
    out = asyncio.run(mcp.dj_play_now([5]))
    assert time.time() - started < 0.5  # early return, not the full cap
    assert "playing=NO" in first_line(out)
    assert "not playing 8s after load" in out and "Do NOT tell Todd it's playing" in out
    assert "should pick this up" not in out


def test_old_build_ack_ok_but_silent_is_never_yes(dev):
    dev["ack"] = lambda s: {"ack_status": "ok"}  # acked on receipt
    dev["state"] = lambda s: {"playing": False, "track": {"id": 5}}  # but silent
    out = asyncio.run(mcp.dj_play_now([5]))
    assert "playing=NO" in first_line(out)
    assert "should pick this up" not in out


def test_old_build_ack_ok_no_fresh_state_is_unconfirmed(dev):
    dev["ack"] = lambda s: {"ack_status": "ok"}
    out = asyncio.run(mcp.dj_play_now([5]))
    assert "playing=UNCONFIRMED" in first_line(out)
    assert "should pick this up" not in out


def test_still_playing_old_song_is_no(dev):
    dev["ack"] = lambda s: {"ack_status": "ok"}
    dev["state"] = playing(99, "Old Song")
    out = asyncio.run(mcp.dj_play_now([5]))
    assert "playing=NO" in first_line(out) and "Old Song" in out


def test_late_honest_ack_is_waited_for(dev):
    # The ack lands after 0.4 s here, standing in for the phone's 8 s start
    # timeout that the old 8 s MCP wait used to miss.
    dev["ack"] = lambda s: {"ack_status": "ok"} if s > 0.4 else None
    dev["state"] = lambda s: {"playing": True, "track": {"id": 5}} if s > 0.4 else None
    out = asyncio.run(mcp.dj_play_now([5]))
    assert "playing=YES" in first_line(out)


def test_no_ack_at_all_is_unconfirmed(dev):
    out = asyncio.run(mcp.dj_play_now([5]))
    assert "playing=UNCONFIRMED" in first_line(out)
    assert "no answer from the device" in out


def test_stream_needs_the_stream_item(dev):
    dev["ack"] = lambda s: {"ack_status": "ok"}
    dev["state"] = playing(-1, "RFL")
    out = asyncio.run(mcp.dj_play_stream("http://x/stream.mp3", "RFL"))
    assert "playing=YES" in first_line(out)
    assert "Playing stream" not in out  # prose never claims it


def test_queue_on_a_live_player_is_yes(dev):
    dev["ack"] = lambda s: {"ack_status": "ok"}
    dev["state"] = playing(3)
    out = asyncio.run(mcp.dj_queue([8, 9]))
    assert "playing=YES" in first_line(out)


def test_pool_start_path_gets_a_verdict(dev, monkeypatch):
    dev["ack"] = lambda s: {"ack_status": "failed", "ack_detail": "player error: 404"}
    out = asyncio.run(mcp._trim_to_pool([4, 5], {"track": None, "queue": []}))
    assert "playing=NO" in out and "player error: 404" in out
    assert "starts now" not in out


def test_transfer_verdict_reads_the_target_device(dev, monkeypatch):
    seen = []
    orig = mcp._get

    async def spy(path):
        seen.append(path)
        return await orig(path)

    monkeypatch.setattr(mcp, "_get", spy)
    dev["state"] = playing(5)
    monkeypatch.setattr(mcp, "_post", lambda *a: asyncio.sleep(0, {"devices": []}))

    async def devices(path):
        if path == "/api/playback/devices":
            return {"devices": [{"id": "e2e-test-device", "name": "E2E"}]}
        return await spy(path)

    monkeypatch.setattr(mcp, "_get", devices)
    out = asyncio.run(mcp.dj_transfer("e2e-test-device"))
    assert out.startswith("RESULT playing=YES on 'e2e-test-device'")
    assert any("device_id=e2e-test-device" in p for p in seen)
