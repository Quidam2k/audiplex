# #2345: lifting Todd's DJ stop retires the stop's own still-undelivered pause.
# 2026-11-11 19:41Z: the pause sat behind 7 commands, ran 1.3 s after the lift, mix paused 57 s.
import time

import pytest

from audiplex import scheduled_stop, todd_stop
from audiplex.playback_bus import bus


@pytest.fixture(autouse=True)
def fresh_bus_and_latch(monkeypatch, tmp_path):  # #2345
    bus.reset()
    scheduled_stop.controller.reset()
    monkeypatch.setattr(todd_stop, "LATCH_PATH", tmp_path / "todd_stop.json")
    yield
    bus.reset()
    scheduled_stop.controller.reset()


def _owed_types():  # #2345 what the phone would still get, in order
    out = []
    now = time.time()
    while True:
        rec = bus._claim(now, "phone")
        if rec is None:
            return out
        out.append((rec.type, rec.source))
        bus.ack(rec.id, "ok", "")


def test_lift_retires_queued_stop_pause_2345(client):
    for i in range(7):  # the 19:41 backlog ahead of the stop's pause
        bus._enqueue("seek", {"position_ms": i}, source="admin/dj-agent:dj_seek")
    r = client.post("/api/playback/stop-latch", json={"reason": "t", "todd_quote": "stop the music"})
    assert r.status_code == 200, r.text
    pause_id = r.json()["pause_id"]
    assert bus.command(pause_id).status == "queued"
    r = client.delete("/api/playback/stop-latch", params={"todd_quote": "ok play it again"})
    assert r.status_code == 200, r.text
    assert r.json()["retired_stop_pauses"] == 1
    rec = bus.command(pause_id)
    assert rec.status == "failed" and rec.ack_status == "superseded"
    assert ("pause", "todd_stop") not in _owed_types()  # never reaches the phone after the lift


def test_lift_retires_delivered_unacked_stop_pause_2345(client):  # it would be redelivered after 60 s
    r = client.post("/api/playback/stop-latch", json={"reason": "t", "todd_quote": "stop"})
    pause_id = r.json()["pause_id"]
    rec = bus._claim(time.time(), "phone")
    assert rec is not None and rec.id == pause_id and rec.status == "delivered"
    client.delete("/api/playback/stop-latch", params={"todd_quote": "go"})
    assert bus.command(pause_id).ack_status == "superseded"
    assert bus._claim(time.time() + 3600, "phone") is None


def test_lift_leaves_acked_pause_and_other_pauses_alone_2345(client):
    r = client.post("/api/playback/stop-latch", json={"reason": "t", "todd_quote": "stop"})
    pause_id = r.json()["pause_id"]
    rec = bus._claim(time.time(), "phone")
    bus.ack(rec.id, "ok", "")
    other = bus._enqueue("pause", {}, source="admin/dj-agent:dj_pause")  # not the stop's pause
    r = client.delete("/api/playback/stop-latch", params={"todd_quote": "go"})
    assert r.json()["retired_stop_pauses"] == 0
    assert bus.command(pause_id).ack_status == "ok"
    assert bus.command(other.id).status == "queued"
