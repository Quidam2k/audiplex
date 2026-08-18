"""Command registry, delivery ACK, redelivery and link history (#900 Phase 3a).

The behaviour under test is the answer to the 2026-08-14 non-delivery: a
command used to be destroyed by the queue pop that delivered it, so a client
that took it and then dropped it left no trace anywhere, and the DJ could
never say more than "queued, hopefully".
"""

import time

import pytest
from fastapi import Depends

from audiplex import routers
from audiplex.auth import get_current_user, hash_password
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import PlayStat, User
from audiplex.playback_bus import (
    STATUS_ACKED,
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_QUEUED,
    bus,
    read_link_history,
)


@pytest.fixture(autouse=True)
def reset_bus():
    bus.reset()
    yield


class TestDeliveryDoesNotConsume:
    def test_delivered_command_survives_delivery(self, client):
        client.post("/api/playback/command", json={"type": "play_now", "payload": {}})
        client.get("/api/playback/command/next")

        rec = bus.command(1)
        assert rec is not None, "delivery must not destroy the record"
        assert rec.status == STATUS_DELIVERED
        assert rec.delivery_count == 1

    def test_delivery_count_reported_to_client(self, client):
        client.post("/api/playback/command", json={"type": "play_now", "payload": {}})
        body = client.get("/api/playback/command/next").json()
        assert body["delivery_count"] == 1

    def test_pending_vs_outstanding(self, client):
        client.post("/api/playback/command", json={"type": "a", "payload": {}})
        assert bus.pending() == 1
        assert bus.outstanding() == 1

        client.get("/api/playback/command/next")
        # Handed over but not acked: no longer pending, still outstanding —
        # this is precisely the state that used to be invisible.
        assert bus.pending() == 0
        assert bus.outstanding() == 1

    def test_commands_endpoint_lists_status(self, client):
        client.post("/api/playback/command", json={"type": "play_now", "payload": {}})
        client.get("/api/playback/command/next")
        rows = client.get("/api/playback/commands").json()
        assert len(rows) == 1
        assert rows[0]["status"] == STATUS_DELIVERED
        assert rows[0]["type"] == "play_now"


class TestAck:
    def test_ack_ok_marks_acked(self, client):
        client.post("/api/playback/command", json={"type": "play_now", "payload": {}})
        client.get("/api/playback/command/next")

        resp = client.post("/api/playback/command/1/ack", json={"status": "ok"})
        assert resp.status_code == 200
        assert resp.json()["status"] == STATUS_ACKED
        assert bus.outstanding() == 0

    def test_ack_failure_is_recorded_not_swallowed(self, client):
        """A device owning up to 'no_tracks' is the whole point of the ACK."""
        client.post("/api/playback/command", json={"type": "play_now", "payload": {}})
        client.get("/api/playback/command/next")

        resp = client.post(
            "/api/playback/command/1/ack",
            json={"status": "no_tracks", "detail": "resolved 0 of 2 track ids"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == STATUS_FAILED
        assert body["ack_status"] == "no_tracks"
        assert body["ack_detail"] == "resolved 0 of 2 track ids"
        # Failed is terminal: a command the device refused must not loop forever.
        assert bus.outstanding() == 0

    def test_ack_unknown_command_404s(self, client):
        resp = client.post("/api/playback/command/999/ack", json={"status": "ok"})
        assert resp.status_code == 404

    def test_device_status_surfaces_ack(self, client):
        client.post("/api/playback/command", json={"type": "play_now", "payload": {}})
        client.get("/api/playback/command/next")
        client.post("/api/playback/command/1/ack", json={"status": "ok"})

        status = client.get("/api/playback/device").json()
        assert status["last_command_status"] == STATUS_ACKED
        assert status["last_command_ack_status"] == "ok"
        assert status["outstanding"] == 0


class TestRedelivery:
    def test_unacked_command_is_redelivered(self, client, monkeypatch):
        """At-least-once: silence from the device means try again (ruling Q2)."""
        monkeypatch.setattr("audiplex.playback_bus.REDELIVER_AFTER_SECONDS", 0.0)
        client.post("/api/playback/command", json={"type": "play_now", "payload": {}})

        first = client.get("/api/playback/command/next").json()
        second = client.get("/api/playback/command/next").json()

        assert first["id"] == second["id"] == 1
        assert second["delivery_count"] == 2

    def test_acked_command_is_never_redelivered(self, client, monkeypatch):
        monkeypatch.setattr("audiplex.playback_bus.REDELIVER_AFTER_SECONDS", 0.0)
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
        client.post("/api/playback/command", json={"type": "play_now", "payload": {}})
        client.get("/api/playback/command/next")
        client.post("/api/playback/command/1/ack", json={"status": "ok"})

        assert client.get("/api/playback/command/next").status_code == 204

    def test_failed_command_is_never_redelivered(self, client, monkeypatch):
        monkeypatch.setattr("audiplex.playback_bus.REDELIVER_AFTER_SECONDS", 0.0)
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
        client.post("/api/playback/command", json={"type": "play_now", "payload": {}})
        client.get("/api/playback/command/next")
        client.post("/api/playback/command/1/ack", json={"status": "error"})

        assert client.get("/api/playback/command/next").status_code == 204

    def test_not_redelivered_before_the_window(self, client, monkeypatch):
        """A healthy device mid-dispatch must not be handed the same command."""
        monkeypatch.setattr("audiplex.playback_bus.REDELIVER_AFTER_SECONDS", 3600.0)
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
        client.post("/api/playback/command", json={"type": "play_now", "payload": {}})
        client.get("/api/playback/command/next")

        assert client.get("/api/playback/command/next").status_code == 204


class TestLinkHistory:
    def test_first_poll_records_a_resume_not_a_gap(self, client, monkeypatch):
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.01)
        client.get("/api/playback/command/next")

        history = read_link_history()
        assert len(history) == 1
        assert history[0]["event"] == "resumed"
        assert history[0]["after_restart"] is True

    def test_gap_is_recorded(self, client, monkeypatch):
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr("audiplex.playback_bus.LINK_GAP_THRESHOLD_SECONDS", 0.0)
        client.get("/api/playback/command/next")  # resume
        client.get("/api/playback/command/next")  # gap vs the previous poll

        history = read_link_history()
        assert [h["event"] for h in history] == ["resumed", "gap"]
        assert history[1]["seconds"] >= 0

    def test_continuous_polling_records_no_gap(self, client, monkeypatch):
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.01)
        client.get("/api/playback/command/next")
        client.get("/api/playback/command/next")

        events = [h["event"] for h in read_link_history()]
        assert events == ["resumed"], "a healthy beat must not manufacture gaps"

    def test_link_history_endpoint(self, client, monkeypatch):
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.01)
        client.get("/api/playback/command/next")

        resp = client.get("/api/playback/link-history")
        assert resp.status_code == 200
        assert resp.json()[0]["event"] == "resumed"


class TestOwnerScopedTasteReads:
    """#3028: /api/music/most-played is per-CALLER, and the DJ calls as
    dj-agent, which has never played anything — so the DJ's taste reads were
    always empty and it would conclude Todd has no history."""

    @pytest.fixture
    def dj_agent_client(self, client, db_session, monkeypatch):
        monkeypatch.setattr(get_settings(), "dj_owner_username", "testuser")
        agent = User(
            username="dj-agent",
            password_hash=hash_password("unused"),
            display_name="DJ Agent",
            is_admin=False,
        )
        db_session.add(agent)
        db_session.commit()

        def override_as_agent(db=Depends(get_db)):
            return db.query(User).filter(User.username == "dj-agent").first()

        client.app.dependency_overrides[get_current_user] = override_as_agent
        yield client
        client.app.dependency_overrides[get_current_user] = (
            lambda db=Depends(get_db): db.query(User).first()
        )

    @pytest.fixture
    def owner_history(self, db_session, sample_track):
        """Plays and early skips belonging to the OWNER, not the agent."""
        owner = db_session.query(User).filter(User.username == "testuser").first()
        db_session.add_all(
            [
                PlayStat(
                    track_id=sample_track.id,
                    user_id=owner.id,
                    event="complete",
                    played_seconds=200.0,
                ),
                PlayStat(
                    track_id=sample_track.id,
                    user_id=owner.id,
                    event="skip",
                    played_seconds=2.0,
                ),
                PlayStat(
                    track_id=sample_track.id,
                    user_id=owner.id,
                    event="skip",
                    played_seconds=1.0,
                ),
            ]
        )
        db_session.commit()
        return sample_track

    def test_owner_most_played_visible_to_dj_agent(
        self, dj_agent_client, owner_history
    ):
        resp = dj_agent_client.get("/api/playback/most-played")
        assert resp.status_code == 200
        assert [t["id"] for t in resp.json()] == [owner_history.id]

    def test_music_most_played_is_still_empty_for_the_agent(
        self, dj_agent_client, owner_history
    ):
        """The per-caller endpoint is left alone on purpose — it is the
        user-facing view, and this asserts the bug the owner-scoped read fixes."""
        resp = dj_agent_client.get("/api/music/most-played")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_owner_likely_skips_visible_to_dj_agent(
        self, dj_agent_client, owner_history
    ):
        resp = dj_agent_client.get("/api/playback/likely-skips")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["track"]["id"] == owner_history.id
        assert rows[0]["early_skip_count"] == 2

    def test_missing_owner_404s(self, client, monkeypatch):
        monkeypatch.setattr(get_settings(), "dj_owner_username", "nobody-configured")
        assert client.get("/api/playback/most-played").status_code == 404
        assert client.get("/api/playback/likely-skips").status_code == 404
