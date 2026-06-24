"""Tests for the DJ playback command bus + now-playing state endpoints."""

import asyncio

import pytest

from audiplex import routers
from audiplex.playback_bus import bus


@pytest.fixture(autouse=True)
def reset_bus():
    """The bus is a global singleton (single-device v1) — reset between tests."""
    bus._queue = asyncio.Queue()
    bus._seq = 0
    bus._state = None
    bus._state_updated_at = 0.0
    yield


class TestPlaybackCommands:
    def test_post_command_acks(self, client):
        resp = client.post(
            "/api/playback/command",
            json={"type": "play_now", "payload": {"track_ids": [1, 2, 3]}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 1
        assert data["type"] == "play_now"
        assert data["pending"] == 1

    def test_next_returns_queued_command(self, client):
        client.post(
            "/api/playback/command",
            json={"type": "play_now", "payload": {"track_ids": [5]}},
        )
        # Already queued, so the long-poll returns immediately (no 25s wait).
        resp = client.get("/api/playback/command/next")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "play_now"
        assert data["payload"]["track_ids"] == [5]
        assert "created_at" in data

    def test_next_times_out_204(self, client, monkeypatch):
        # Shrink the long-poll so the empty-queue path returns quickly.
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
        resp = client.get("/api/playback/command/next")
        assert resp.status_code == 204

    def test_commands_drain_in_order(self, client):
        client.post("/api/playback/command", json={"type": "a", "payload": {}})
        client.post("/api/playback/command", json={"type": "b", "payload": {}})
        first = client.get("/api/playback/command/next").json()
        second = client.get("/api/playback/command/next").json()
        assert first["type"] == "a"
        assert second["type"] == "b"


class TestPlaybackState:
    def test_get_state_empty(self, client):
        resp = client.get("/api/playback/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["playing"] is False
        assert data["track"] is None

    def test_post_then_get_state(self, client):
        payload = {
            "playing": True,
            "track": {"id": 7, "title": "So What", "artist": "Miles Davis"},
            "position_ms": 12000,
            "duration_ms": 540000,
            "queue_length": 5,
            "queue_index": 2,
        }
        resp = client.post("/api/playback/state", json=payload)
        assert resp.status_code == 200

        resp = client.get("/api/playback/state")
        data = resp.json()
        assert data["playing"] is True
        assert data["track"]["id"] == 7
        assert data["track"]["artist"] == "Miles Davis"
        assert data["queue_index"] == 2
        assert "updated_at" in data
