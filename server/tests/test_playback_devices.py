"""Device registry and active-device targeting behavior."""

import pytest

from audiplex import routers
from audiplex.playback_bus import LEGACY_DEVICE_ID, bus


PC_PARAMS = {
    "device_id": "pc-solace",
    "device_name": "Solace",
    "device_type": "windows",
}
COMMAND = {"type": "play_now", "payload": {}}


def drain_handoff(client):
    """Ack every transfer-handshake command so tests can look past them."""
    # Acking a deactivate queues the activate, so repeat until none are left.
    while pending := [
        rec for rec in bus._commands.values()
        if rec.type in ("deactivate", "activate") and rec.status != "acked"
    ]:
        for rec in pending:
            bus.ack(rec.id, "ok")


def poll(client, params=None):
    resp = client.get("/api/playback/command/next", params=params)
    return resp.json() if resp.status_code == 200 else None


def playing_state(track_ids, index=0, position_ms=0, playing=True):
    return {
        "playing": playing,
        "track": {"id": track_ids[index]},
        "position_ms": position_ms,
        "queue_index": index,
        "queue": [{"index": i, "id": t} for i, t in enumerate(track_ids)],
    }


@pytest.fixture(autouse=True)
def reset_bus():
    bus.reset()
    yield


class TestBackCompat:
    def test_paramless_phone_poll_with_no_pc(self, client):
        client.post("/api/playback/command", json=COMMAND)

        response = client.get("/api/playback/command/next")
        assert response.status_code == 200
        assert response.json()["type"] == "play_now"

        devices = client.get("/api/playback/devices").json()
        assert devices["active_device_id"] is None
        assert len(devices["devices"]) == 1
        assert devices["devices"][0]["id"] == LEGACY_DEVICE_ID

        status = bus.device_status()
        assert status["active_device_id"] is None
        assert status["effective_target_device_id"] is None


class TestTargeting:
    def test_inactive_pc_never_takes_untargeted_commands(self, client, monkeypatch):
        # A PC that is running but not activated must not race the phone (R1).
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
        client.post("/api/playback/command", json=COMMAND)

        assert client.get(
            "/api/playback/command/next", params=PC_PARAMS
        ).status_code == 204
        assert bus.pending() == 1
        assert client.get("/api/playback/command/next").status_code == 200

    def test_active_pc_receives_command_instead_of_phone(self, client, monkeypatch):
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
        assert client.get(
            "/api/playback/command/next", params=PC_PARAMS
        ).status_code == 204

        response = client.post("/api/playback/devices/pc-solace/activate")
        assert response.status_code == 200
        assert response.json()["active_device_id"] == "pc-solace"
        drain_handoff(client)

        client.post("/api/playback/command", json=COMMAND)

        assert client.get("/api/playback/command/next").status_code == 204
        assert bus.pending() == 1

        response = client.get("/api/playback/command/next", params=PC_PARAMS)
        assert response.status_code == 200
        assert response.json()["type"] == "play_now"

    def test_phone_activation_is_always_accepted(self, client):
        response = client.post(
            f"/api/playback/devices/{LEGACY_DEVICE_ID}/activate"
        )
        assert response.status_code == 200
        assert response.json()["active_device_id"] == LEGACY_DEVICE_ID

    def test_registered_device_metadata_is_listed(self, client, monkeypatch):
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
        assert client.get(
            "/api/playback/command/next", params=PC_PARAMS
        ).status_code == 204
        client.post("/api/playback/devices/pc-solace/activate")

        devices = client.get("/api/playback/devices").json()["devices"]
        device = next(row for row in devices if row["id"] == "pc-solace")
        assert device["name"] == "Solace"
        assert device["type"] == "windows"
        assert device["active"] is True

    def test_transfer_back_to_phone(self, client, monkeypatch):
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
        assert client.get(
            "/api/playback/command/next", params=PC_PARAMS
        ).status_code == 204
        client.post("/api/playback/devices/pc-solace/activate")
        drain_handoff(client)
        client.post(f"/api/playback/devices/{LEGACY_DEVICE_ID}/activate")
        drain_handoff(client)
        client.post("/api/playback/command", json=COMMAND)

        response = client.get("/api/playback/command/next")
        assert response.status_code == 200
        assert response.json()["type"] == "play_now"
        assert client.get(
            "/api/playback/command/next", params=PC_PARAMS
        ).status_code == 204

    def test_unknown_device_activation_returns_404(self, client):
        response = client.post("/api/playback/devices/unknown/activate")
        assert response.status_code == 404

    def test_state_report_bumps_device_last_seen(self, client, monkeypatch):
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
        assert client.get(
            "/api/playback/command/next", params=PC_PARAMS
        ).status_code == 204
        bus._devices["pc-solace"].last_seen = 0

        response = client.post(
            "/api/playback/state?device_id=pc-solace",
            json={},
        )
        assert response.status_code == 200
        assert bus._devices["pc-solace"].last_seen > 0


class TestStaleFallback:
    def test_stale_active_pc_falls_back_to_phone(self, client, monkeypatch):
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
        assert client.get(
            "/api/playback/command/next", params=PC_PARAMS
        ).status_code == 204
        client.post("/api/playback/devices/pc-solace/activate")
        monkeypatch.setattr(
            "audiplex.playback_bus.DEVICE_STALE_AFTER_SECONDS", -1.0
        )
        client.post("/api/playback/command", json=COMMAND)

        response = client.get("/api/playback/command/next")
        assert response.status_code == 200
        assert response.json()["type"] == "play_now"

        status = bus.device_status()
        assert status["active_device_id"] == "pc-solace"
        assert status["effective_target_device_id"] is None


class TestPerDeviceState:
    def _state(self, track_id):
        return {"playing": True, "track": {"id": track_id}, "queue": []}

    def test_idle_pc_report_does_not_clobber_phone_state(self, client):
        client.post("/api/playback/state", json=self._state(1))
        client.post("/api/playback/state?device_id=pc-solace", json={})

        assert client.get("/api/playback/state").json()["track"]["id"] == 1

    def test_active_pc_state_is_the_default(self, client, monkeypatch):
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
        client.get("/api/playback/command/next", params=PC_PARAMS)
        client.post("/api/playback/state", json=self._state(1))
        client.post("/api/playback/state?device_id=pc-solace", json=self._state(2))
        client.post("/api/playback/devices/pc-solace/activate")

        assert client.get("/api/playback/state").json()["track"]["id"] == 2
        phone = client.get("/api/playback/state", params={"device_id": "phone"})
        assert phone.json()["track"]["id"] == 1


class TestTransferHandoff:
    @pytest.fixture(autouse=True)
    def short_poll(self, monkeypatch):
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)

    def test_live_phone_is_deactivated_then_pc_resumes_at_position(self, client):
        poll(client)  # phone is live
        poll(client, PC_PARAMS)
        client.post("/api/playback/state", json=playing_state([5, 6, -3, 7], 1, 42000))

        client.post("/api/playback/devices/pc-solace/activate")

        deactivate = poll(client)
        assert deactivate["type"] == "deactivate"
        assert deactivate["payload"]["handoff_to"] == "pc-solace"
        assert poll(client, PC_PARAMS) is None, "activate waits for the deactivate ack"

        # Phone pauses, posts its final position, then acks.
        client.post(
            "/api/playback/state",
            json=playing_state([5, 6, -3, 7], 1, 43500, playing=False),
        )
        client.post(f"/api/playback/command/{deactivate['id']}/ack", json={"status": "ok"})

        activate = poll(client, PC_PARAMS)
        assert activate["type"] == "activate"
        assert activate["payload"] == {
            "from_device": "phone",
            "track_ids": [6, 7],  # from the current track on; the voice clip is dropped
            "position_ms": 43500,
            "playing": True,  # it was playing when the transfer was asked for
        }

    def test_old_app_unknown_type_ack_still_hands_off(self, client):
        poll(client)
        poll(client, PC_PARAMS)
        client.post("/api/playback/state", json=playing_state([5], 0, 1000))
        client.post("/api/playback/devices/pc-solace/activate")
        deactivate = poll(client)
        client.post(
            f"/api/playback/command/{deactivate['id']}/ack",
            json={"status": "unknown_type", "detail": "deactivate"},
        )
        assert poll(client, PC_PARAMS)["type"] == "activate"

    def test_stale_previous_device_hands_off_immediately(self, client, monkeypatch):
        poll(client, PC_PARAMS)
        client.post("/api/playback/devices/pc-solace/activate")
        drain_handoff(client)
        client.post(
            "/api/playback/state?device_id=pc-solace", json=playing_state([9], 0, 5000)
        )
        bus._devices["pc-solace"].last_seen = 0  # PC went to sleep

        client.post(f"/api/playback/devices/{LEGACY_DEVICE_ID}/activate")

        activate = poll(client)
        assert activate["type"] == "activate"
        assert activate["payload"]["track_ids"] == [9]
        assert activate["payload"]["position_ms"] == 5000

    def test_nothing_playing_still_activates_with_empty_queue(self, client):
        poll(client, PC_PARAMS)
        client.post("/api/playback/devices/pc-solace/activate")
        activate = poll(client, PC_PARAMS)
        assert activate["type"] == "activate"
        assert activate["payload"]["track_ids"] == []
        assert activate["payload"]["playing"] is False

    def test_reactivating_same_device_sends_no_handoff(self, client):
        poll(client, PC_PARAMS)
        client.post("/api/playback/devices/pc-solace/activate")
        drain_handoff(client)
        client.post("/api/playback/devices/pc-solace/activate")
        assert poll(client, PC_PARAMS) is None
