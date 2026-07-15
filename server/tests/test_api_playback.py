"""Tests for the DJ playback command bus + now-playing state endpoints."""

import asyncio

import pytest
from fastapi import Depends

from audiplex import routers
from audiplex.auth import get_current_user, hash_password
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import Playlist, PlaylistTrack, User
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

    def test_volume_round_trips(self, client):
        payload = {
            "playing": True,
            "track": None,
            "position_ms": 0,
            "duration_ms": 0,
            "queue_length": 0,
            "queue_index": 0,
            "volume": 0.4,
        }
        resp = client.post("/api/playback/state", json=payload)
        assert resp.status_code == 200
        assert resp.json()["volume"] == 0.4

        resp = client.get("/api/playback/state")
        assert resp.json()["volume"] == 0.4

    def test_state_without_volume_defaults_none(self, client):
        resp = client.get("/api/playback/state")
        assert resp.json()["volume"] is None


class TestOwnerResolvedLibraryReads:
    """The dj-agent service account has no playlists/favorites of its own —
    these endpoints must resolve to settings.dj_owner_username regardless."""

    @pytest.fixture
    def dj_agent_client(self, client, db_session, monkeypatch):
        """Same TestClient, but authenticated as a second user ('dj-agent')
        while settings.dj_owner_username points at the original 'testuser'."""
        monkeypatch.setattr(get_settings(), "dj_owner_username", "testuser")

        agent = User(
            username="dj-agent",
            password_hash=hash_password("unused"),
            display_name="DJ Agent",
            is_admin=False,
        )
        db_session.add(agent)
        db_session.commit()
        db_session.refresh(agent)

        def override_as_agent(db=Depends(get_db)):
            return db.query(User).filter(User.username == "dj-agent").first()

        client.app.dependency_overrides[get_current_user] = override_as_agent
        yield client
        client.app.dependency_overrides[get_current_user] = lambda db=Depends(get_db): db.query(User).first()

    def test_owner_playlists_visible_to_dj_agent(self, dj_agent_client, db_session, sample_playlist):
        # sample_playlist is owned by the original 'testuser' (the configured owner).
        resp = dj_agent_client.get("/api/playback/playlists")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()]
        assert "Test Playlist" in names

    def test_owner_playlist_detail_visible_to_dj_agent(self, dj_agent_client, sample_playlist, sample_track):
        resp = dj_agent_client.get(f"/api/playback/playlists/{sample_playlist.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sample_playlist.id
        assert [t["id"] for t in data["tracks"]] == [sample_track.id]

    def test_missing_owner_404s(self, client, monkeypatch):
        monkeypatch.setattr(get_settings(), "dj_owner_username", "nobody-configured")
        resp = client.get("/api/playback/playlists")
        assert resp.status_code == 404
