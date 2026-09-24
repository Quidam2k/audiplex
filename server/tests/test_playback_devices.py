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
    def test_active_pc_receives_command_instead_of_phone(self, client, monkeypatch):
        monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
        assert client.get(
            "/api/playback/command/next", params=PC_PARAMS
        ).status_code == 204

        response = client.post("/api/playback/devices/pc-solace/activate")
        assert response.status_code == 200
        assert response.json()["active_device_id"] == "pc-solace"

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
        client.post(f"/api/playback/devices/{LEGACY_DEVICE_ID}/activate")
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
