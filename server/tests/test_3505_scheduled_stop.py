"""#3505: verified server-side stops + the stop latch.

The 2026-09-30 failure: dj_sleep_timer went to a phone that answered
unknown_type, nothing noticed, and the personas kept appending. These drive
StopController.tick() with a fake clock against the real bus.
"""

import pytest

from audiplex import scheduled_stop
from audiplex.playback_bus import bus
from audiplex.scheduled_stop import StopController


@pytest.fixture(autouse=True)
def fresh():
    bus.reset()
    scheduled_stop.controller.reset()
    yield
    scheduled_stop.controller.reset()


def _state(playing=True, track_id=7, pos_ms=0, dur_ms=180_000, volume=0.8, at=None):
    """Report a state; `at` stamps it on the test's fake clock."""
    bus.set_state({
        "playing": playing,
        "track": {"id": track_id, "title": f"T{track_id}", "artist": "A"},
        "position_ms": pos_ms, "duration_ms": dur_ms,
        "queue_length": 3, "queue_index": 0, "queue": [], "volume": volume,
    })
    if at is not None:
        key = next(iter(bus._states))
        bus._states[key] = (bus._states[key][0], at)
    return bus.get_state()["updated_at"]


def _types():
    return [c["type"] for c in bus.commands(200)]


def _last(cmd_type):
    return [c for c in bus.commands(200) if c["type"] == cmd_type][-1]


class TestAfterCurrent:
    def test_trim_ok_then_device_stops_is_yes(self):
        c = StopController()
        t0 = _state(pos_ms=170_000)
        r = c.arm_after_current(bus, t0)
        # #6913: the trim, then the exact end-of-song stop for 1.0.52+ phones.
        assert r["ok"] and _types() == ["replace_upcoming", "stop_after_current"]
        assert c.latch_active(t0)
        bus.ack(_last("replace_upcoming")["id"], "ok")
        c.tick(bus, t0 + 1)
        assert c.job["trim"] == "ok" and c.job["phase"] == "await_end"
        # Trimmed: no pause near the end; the song just ends.
        c.tick(bus, t0 + 9.5)
        assert "pause" not in _types()
        _state(playing=False, pos_ms=180_000, at=t0 + 10.5)
        c.tick(bus, t0 + 11)
        assert c.job is None and c.last["verdict"] == "YES"
        assert "playing=no" in c.last["reason"]

    def test_old_phone_unknown_type_pauses_at_end(self):
        c = StopController()
        t0 = _state(pos_ms=170_000)
        c.arm_after_current(bus, t0)
        bus.ack(_last("replace_upcoming")["id"], "unknown_type", "replace_upcoming")
        c.tick(bus, t0 + 1)
        assert c.job["trim"].startswith("unknown_type")
        c.tick(bus, t0 + 5)  # 5 s left: not yet
        assert "pause" not in _types()
        c.tick(bus, t0 + 9.2)  # < 1 s left: pause
        assert _types()[-1] == "pause" and c.job["phase"] == "verifying"
        bus.ack(_last("pause")["id"], "ok")
        _state(playing=False, pos_ms=179_300, at=t0 + 9.8)
        c.tick(bus, t0 + 10)
        assert c.last["verdict"] == "YES"

    def test_song_changed_first_pauses_immediately(self):
        c = StopController()
        t0 = _state(pos_ms=10_000)
        c.arm_after_current(bus, t0)
        _state(track_id=8, pos_ms=0)
        c.tick(bus, t0 + 2)
        assert _types()[-1] == "pause"
        assert "song changed" in c.job["action"]

    def test_never_reports_stopped_is_no(self):
        c = StopController()
        t0 = _state(pos_ms=179_500)
        c.arm_after_current(bus, t0)
        c.tick(bus, t0 + 0.1)  # trim unanswered, <1 s left -> pause
        assert _types()[-1] == "pause"
        c.tick(bus, t0 + 0.1 + scheduled_stop.VERIFY_WINDOW_S + 1)
        assert c.last["verdict"] == "NO"
        assert "within" in c.last["reason"]

    def test_pause_refused_is_no(self):
        c = StopController()
        t0 = _state(pos_ms=179_500)
        c.arm_after_current(bus, t0)
        c.tick(bus, t0 + 0.1)
        bus.ack(_last("pause")["id"], "error", "boom")
        c.tick(bus, t0 + 1)
        assert c.last["verdict"] == "NO" and "refused pause" in c.last["reason"]

    def test_nothing_playing_refuses(self):
        c = StopController()
        t0 = _state(playing=False)
        assert c.arm_after_current(bus, t0)["ok"] is False
        assert _types() == []

    def test_stream_refuses(self):
        c = StopController()
        t0 = _state(track_id=-1, dur_ms=0)
        r = c.arm_after_current(bus, t0)
        assert r["ok"] is False and "dj_pause" in r["error"]


class TestFade:
    def test_fade_steps_pause_restore_and_verify(self):
        c = StopController()
        t0 = _state(volume=0.8)
        c.arm_fade(bus, t0, minutes=1, fade_seconds=10)
        c.tick(bus, t0 + 30)
        assert _types() == []  # still waiting
        c.tick(bus, t0 + 50)   # fade starts
        c.tick(bus, t0 + 55)
        vols = [x for x in bus.commands(200) if x["type"] == "volume"]
        assert len(vols) >= 2
        c.tick(bus, t0 + 60.1)
        assert _types()[-1] == "pause" and c.latch_active(t0 + 61)
        _state(playing=False, at=t0 + 60.5)
        c.tick(bus, t0 + 61)
        assert c.last["verdict"] == "YES"
        # Volume restored after the pause.
        assert _types()[-1] == "volume"
        assert bus.command(_last("volume")["id"]).payload["volume"] == 0.8

    def test_cancel_mid_fade_restores_volume(self):
        c = StopController()
        t0 = _state(volume=0.5)
        c.arm_fade(bus, t0, minutes=0.5, fade_seconds=20)
        c.tick(bus, t0 + 12)
        assert c.job["phase"] == "fading"
        c.clear("test", bus, t0 + 13)
        assert c.last["verdict"] == "CANCELLED"
        assert bus.command(_last("volume")["id"]).payload["volume"] == 0.5


class TestLatch:
    def test_refill_refused_until_explicit_start(self):
        c = StopController()
        c.set_latch("stop after X", 100.0)
        msg = c.gate("queue", {"track_ids": [1]}, 101.0)
        assert msg and msg.startswith("STOPPED") and "dj_play_now" in msg
        assert c.gate("pause", {}, 101.0) is None
        assert c.gate("play_now", {"track_ids": [1]}, 102.0) is None
        assert c.gate("queue", {"track_ids": [1]}, 103.0) is None

    def test_latch_expires(self):
        c = StopController()
        c.set_latch("x", 0.0)
        assert c.latch_active(scheduled_stop.LATCH_TTL_S - 1)
        assert not c.latch_active(scheduled_stop.LATCH_TTL_S + 1)

    def test_pool_does_not_top_up_while_latched(self, monkeypatch):
        from audiplex.dj_pool import get_pool

        called = []
        pool = get_pool()
        monkeypatch.setattr(pool, "is_active", lambda: True)
        monkeypatch.setattr(pool, "top_up", lambda **kw: called.append(kw) or {"picks": [9]})
        scheduled_stop.controller.set_latch("x", __import__("time").time())
        _state(track_id=11)
        assert called == [] and "queue" not in _types()


class TestRoutes:
    def test_arm_status_latch_409_and_delete(self, client):
        client.post("/api/playback/state", json={
            "playing": True, "track": {"id": 7, "title": "Take On Me", "artist": "RBF"},
            "position_ms": 1000, "duration_ms": 180000, "queue_length": 5, "queue_index": 0})
        r = client.post("/api/playback/scheduled-stop", json={"mode": "after_current"})
        assert r.status_code == 200 and r.json()["latched"] is True
        s = client.get("/api/playback/state").json()
        assert s["stop"]["latched"] and "Take On Me" in s["stop"]["latch_reason"]
        assert s["stop"]["job"]["ends_in_s"] > 170
        q = client.post("/api/playback/command", json={"type": "queue", "payload": {"track_ids": [1]}})
        assert q.status_code == 409 and "STOPPED" in q.json()["detail"]
        d = client.delete("/api/playback/scheduled-stop").json()
        assert d["cleared"] and d["latched"] is False
        q = client.post("/api/playback/command", json={"type": "queue", "payload": {"track_ids": [1]}})
        assert q.status_code == 200

    def test_arm_with_nothing_playing_is_409(self, client):
        r = client.post("/api/playback/scheduled-stop", json={"mode": "after_current"})
        assert r.status_code == 409

    def test_app_version_round_trips(self, client):
        client.post("/api/playback/state", json={
            "playing": False, "app_version_name": "1.0.49", "app_version_code": 49})
        s = client.get("/api/playback/state").json()
        assert s["app_version_name"] == "1.0.49" and s["app_version_code"] == 49
