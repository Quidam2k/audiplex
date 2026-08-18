"""Tests for the DJ playback command bus + now-playing state endpoints."""

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
    bus.reset()
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


class TestDeviceLiveness:
    """#2961: a dead player and an idle one used to look identical."""

    def test_no_device_before_any_poll(self, client):
        data = client.get("/api/playback/device").json()
        assert data["connected"] is False
        assert data["last_poll_at"] is None
        assert data["ever_reported_state"] is False

    def test_poll_marks_device_connected(self, client):
        client.post("/api/playback/command", json={"type": "skip", "payload": {}})
        client.get("/api/playback/command/next")

        data = client.get("/api/playback/device").json()
        assert data["connected"] is True
        assert data["last_poll_age_seconds"] < 5
        assert data["last_command_id"] == 1
        assert data["last_command_type"] == "skip"
        assert data["last_command_delivered_age_seconds"] < 5

    def test_idle_device_is_connected_but_never_reported(self, client):
        """The distinction that matters: polling, alive, nothing playing."""
        client.post("/api/playback/command", json={"type": "skip", "payload": {}})
        client.get("/api/playback/command/next")

        data = client.get("/api/playback/device").json()
        assert data["connected"] is True
        assert data["ever_reported_state"] is False
        assert data["last_command_delivered_at"] is not None

    def test_stale_poll_is_not_connected(self, client):
        client.post("/api/playback/command", json={"type": "skip", "payload": {}})
        client.get("/api/playback/command/next")
        # Rewind the last beat past the staleness window.
        bus._last_poll_at -= 120.0

        data = client.get("/api/playback/device").json()
        assert data["connected"] is False
        assert data["last_poll_age_seconds"] > 60


class TestClientLog:
    def test_post_then_read_back(self, client):
        resp = client.post(
            "/api/playback/client-log",
            json={
                "level": "error",
                "event": "player_error",
                "message": "Source error",
                "detail": {"code": "ERROR_CODE_IO_BAD_HTTP_STATUS"},
                "at": 1786723388.0,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == 1
        assert resp.json()["received_at"] > 0

        entries = client.get("/api/playback/client-log").json()
        assert len(entries) == 1
        assert entries[0]["event"] == "player_error"
        assert entries[0]["detail"]["code"] == "ERROR_CODE_IO_BAD_HTTP_STATUS"
        assert entries[0]["at"] == 1786723388.0

    def test_oldest_first_and_limit_keeps_newest(self, client):
        for i in range(5):
            client.post(
                "/api/playback/client-log",
                json={"event": "process_exit", "message": f"exit {i}"},
            )
        entries = client.get("/api/playback/client-log", params={"limit": 2}).json()
        assert [e["message"] for e in entries] == ["exit 3", "exit 4"]

    def test_buffer_is_bounded(self, client):
        from audiplex.playback_bus import CLIENT_LOG_CAPACITY

        for i in range(CLIENT_LOG_CAPACITY + 10):
            bus.add_client_log({"event": "spam", "message": str(i)})
        entries = client.get("/api/playback/client-log", params={"limit": 200}).json()
        assert len(entries) == CLIENT_LOG_CAPACITY
        assert entries[-1]["message"] == str(CLIENT_LOG_CAPACITY + 9)


class TestPersistedExits:
    """#3021: a process-exit report must survive a server restart.

    The phone advances its own report watermark as soon as we accept an entry,
    so anything held only in the ring buffer is gone for good if the process
    restarts before a human reads it — the phone will never re-send it.
    """

    def test_process_exit_is_persisted_and_readable(self, client):
        client.post(
            "/api/playback/client-log",
            json={
                "event": "process_exit",
                "message": "SIGNALED",
                "detail": {"signal": "SIGKILL", "trace": "at Foo.bar()"},
            },
        )
        entries = client.get("/api/playback/client-exits").json()
        assert len(entries) == 1
        assert entries[0]["message"] == "SIGNALED"
        assert entries[0]["detail"]["trace"] == "at Foo.bar()"

    def test_persisted_exits_survive_a_bus_reset(self, client):
        client.post(
            "/api/playback/client-log",
            json={"event": "process_exit", "message": "LOW_MEMORY"},
        )
        bus.reset()  # stands in for a restart: memory gone, disk intact
        assert client.get("/api/playback/client-log").json() == []
        entries = client.get("/api/playback/client-exits").json()
        assert [e["message"] for e in entries] == ["LOW_MEMORY"]

    def test_only_exits_are_persisted(self, client):
        client.post(
            "/api/playback/client-log",
            json={"event": "player_error", "message": "boom"},
        )
        assert client.get("/api/playback/client-exits").json() == []

    def test_missing_file_reads_as_empty(self, client):
        assert client.get("/api/playback/client-exits").json() == []


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

    def test_owner_ratings_visible_to_dj_agent(self, dj_agent_client, db_session, sample_track):
        """The DJ must see Todd's stars, not its own empty account (#3024).

        dj-agent has never rated anything, so a per-caller read would return
        [] and the DJ would conclude Todd has no opinions. The rating is
        seeded straight onto the configured owner, since the dj_agent_client
        fixture makes every request authenticate as the agent.
        """
        from audiplex.models import TrackRating

        owner = db_session.query(User).filter(User.username == "testuser").first()
        db_session.add(
            TrackRating(
                user_id=owner.id,
                track_id=sample_track.id,
                rating=5,
                note="peak driving music",
            )
        )
        db_session.commit()

        resp = dj_agent_client.get("/api/playback/ratings")
        assert resp.status_code == 200
        ratings = resp.json()
        assert [r["track_id"] for r in ratings] == [sample_track.id]
        assert ratings[0]["rating"] == 5

    def test_missing_owner_404s(self, client, monkeypatch):
        monkeypatch.setattr(get_settings(), "dj_owner_username", "nobody-configured")
        resp = client.get("/api/playback/playlists")
        assert resp.status_code == 404
