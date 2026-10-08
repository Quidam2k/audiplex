# #4011: Todd's DJ stop latch (dj_stop / dj_stop_lift). Codex draft, reviewed by hand.
import importlib
import json
import time

import pytest

from audiplex import scheduled_stop, todd_stop
from audiplex.playback_bus import bus

OWNER = "admin"
AGENT = "admin/dj-agent:dj_play_now"
START_TYPES = (
    "play_now", "resume", "queue", "play_next", "play_stream",
    "play_book", "activate", "announce", "bed_play",
)


@pytest.fixture(autouse=True)
def fresh_bus_and_latch(monkeypatch, tmp_path):
    bus.reset()
    scheduled_stop.controller.reset()
    monkeypatch.setattr(todd_stop, "LATCH_PATH", tmp_path / "todd_stop.json")
    yield
    bus.reset()
    scheduled_stop.controller.reset()


def test_agent_starts_are_refused_and_never_delivered():
    """#4011: Agent start commands must never bypass Todd's stop."""
    todd_stop.set_latch("t", "stop it", 1000.0)
    assert set(START_TYPES) <= todd_stop.START_CMDS
    for cmd_type in START_TYPES:
        rec = bus._enqueue(
            cmd_type, {"track_ids": [1]}, source="admin/dj-agent:dj_queue"
        )
        assert rec.status == "failed"
        assert rec.ack_status == "refused"
        assert bus._claim(time.time(), "phone") is None


def test_internal_sources_cannot_bypass_stop():
    """#4011: Pool, transfer, and trigger starts must also be refused."""
    todd_stop.set_latch("t", "stop it", 1000.0)
    for source in ("dj_pool:top_up", "transfer", "dj_triggers:outro"):
        for cmd_type in START_TYPES:
            rec = bus._enqueue(cmd_type, {"track_ids": [1]}, source=source)
            assert rec.status == "failed"
            assert rec.ack_status == "refused"
            assert bus._claim(time.time(), "phone") is None


def test_replace_upcoming_allows_trim_but_refuses_refill():
    """#4011: Empty queue trims must remain possible while refills stop."""
    todd_stop.set_latch("t", "stop it", 1000.0)
    trim = bus._enqueue("replace_upcoming", {"track_ids": []}, source=AGENT)
    assert trim.status != "failed"
    assert trim.ack_status != "refused"
    refill = bus._enqueue("replace_upcoming", {"track_ids": [5]}, source=AGENT)
    assert refill.status == "failed"
    assert refill.ack_status == "refused"


def test_stop_and_control_commands_remain_allowed():
    """#4011: Todd's stop must not block pause, volume, or timer controls."""
    todd_stop.set_latch("t", "stop it", 1000.0)
    for cmd_type in (
        "pause", "volume", "stop_after_current",
        "cancel_stop_after_current", "sleep_timer",
    ):
        rec = bus._enqueue(cmd_type, {}, source=AGENT)
        assert rec.status != "failed"
        assert rec.ack_status != "refused"
    rec = bus._enqueue("pause", {}, source="todd_stop")
    assert rec.status != "failed"
    assert rec.ack_status != "refused"


def test_exact_owner_web_source_can_play():
    """#4011: Todd's exact web source must retain direct playback control."""
    todd_stop.set_latch("t", "stop it", 1000.0)
    rec = bus._enqueue("play_now", {"track_ids": [1]}, source=OWNER)
    assert rec.status != "failed"
    assert rec.ack_status != "refused"
    assert bus._claim(time.time(), "phone") is rec


def test_preexisting_starts_are_refused_at_delivery_and_redelivery():
    """#4011: Queued and unacked starts must not escape after the latch."""
    queued = bus._enqueue("play_now", {"track_ids": [1]}, source=AGENT)
    assert queued.status == "queued"
    todd_stop.set_latch("t", "stop it", 1000.0)
    assert bus._claim(time.time(), "phone") is None
    assert queued.status == "failed"
    assert queued.ack_status == "refused"
    assert queued.delivery_count == 0

    todd_stop.lift("play again", 1001.0)
    delivered = bus._enqueue("play_now", {"track_ids": [1]}, source=AGENT)
    assert bus._claim(time.time(), "phone") is delivered
    assert delivered.status == "delivered"
    todd_stop.set_latch("t", "stop again", 1002.0)
    delivered.delivered_at -= 120.0
    assert bus._claim(time.time(), "phone") is None
    assert delivered.status == "failed"
    assert delivered.ack_status == "refused"
    assert delivered.delivery_count == 1


def test_cancel_outstanding_starts_counts_queued_and_unacked_only():
    """#4011: Cancelling starts must include retries and preserve pauses."""
    acked = bus._enqueue("play_now", {"track_ids": [1]}, source=AGENT)
    assert bus._claim(time.time(), "phone") is acked
    bus.ack(acked.id, "ok", "played")
    delivered = bus._enqueue("resume", {}, source=AGENT)
    assert bus._claim(time.time(), "phone") is delivered
    queued = [  # distinct ids: the #7335 queue guard would drop repeats of track 1
        bus._enqueue(kind, {"track_ids": [100 + i]}, source="admin")
        for i, kind in enumerate(sorted(todd_stop.START_CMDS))
    ]
    pause = bus._enqueue("pause", {}, source=AGENT)
    assert bus.cancel_outstanding_starts() == len(queued) + 1
    for rec in [delivered, *queued]:
        assert rec.status == "failed"
        assert rec.ack_status == "refused"
    assert acked.status == "acked"
    assert pause.status == "queued"
    assert bus.cancel_outstanding_starts() == 0
    assert bus._claim(time.time(), "phone") is pause


def test_quotes_are_required_and_valid_lift_is_recorded():
    """#4011: Unquoted actions must not set or lift Todd's stop."""
    for quote in ("", "   "):
        with pytest.raises(ValueError):
            todd_stop.set_latch("t", quote, 1000.0)
    todd_stop.set_latch("t", "stop it", 1000.0)
    for quote in ("", "   "):
        with pytest.raises(ValueError):
            todd_stop.lift(quote, 1001.0)
        assert todd_stop.is_latched()
    todd_stop.lift("play again", 1002.0)
    assert todd_stop.is_latched() is False
    state = json.loads(todd_stop.LATCH_PATH.read_text(encoding="utf-8"))
    assert any(
        event["event"] == "lift" and event["quote"] == "play again"
        for event in state["history"]
    )


def test_latch_persists_across_module_reload_and_singleton_resets(monkeypatch):
    """#4011: Module reloads and bus/controller resets must preserve the stop."""
    todd_stop.set_latch("t", "stop it", 1000.0)
    path = todd_stop.LATCH_PATH
    assert path.is_file()
    importlib.reload(todd_stop)
    monkeypatch.setattr(todd_stop, "LATCH_PATH", path)
    assert todd_stop.is_latched()
    message = todd_stop.refusal(1001.0)
    assert "stop it" in message
    assert "Only Todd lifts it" in message
    bus.reset()
    scheduled_stop.controller.reset()
    assert todd_stop.is_latched()


def test_scheduled_stop_resume_delete_and_play_now_do_not_lift():
    """#4011: Scheduled-stop clearing actions must not clear Todd's latch."""
    todd_stop.set_latch("t", "stop it", 1000.0)
    controller = scheduled_stop.controller
    for action in ("resume", "delete", "play_now"):
        controller.latch = {
            "reason": "scheduled stop", "set_at": 1000.0, "expires_at": 2000.0
        }
        if action == "delete":
            controller.clear("cancelled by DELETE", bus, 1001.0)
        else:
            assert controller.gate(action, {"track_ids": [1]}, 1001.0, bus) is None
        assert controller.latch is None
        assert todd_stop.is_latched()
        rec = bus._enqueue("resume", {}, source=AGENT)
        assert rec.status == "failed"
        assert rec.ack_status == "refused"


def test_latch_never_times_out(monkeypatch):
    """#4011: Elapsed time must never expire Todd's persistent stop."""
    todd_stop.set_latch("t", "stop it", 1000.0)
    later = 1000.0 + 10 * 86400
    monkeypatch.setattr(time, "time", lambda: later)
    assert todd_stop.is_latched()
    assert todd_stop.refusal(later) is not None
    assert todd_stop.gate("resume", {}, AGENT, OWNER, later) is not None


def test_phone_playing_reports_do_not_lift(monkeypatch):
    """#4011: Bluetooth autoplay reports must not lift or rewrite the latch."""
    todd_stop.set_latch("t", "stop it", 1000.0)
    prior = todd_stop.read_latch()
    state = {
        "playing": True, "track": {"id": 7, "title": "x", "artist": "y"},
        "position_ms": 0, "duration_ms": 180000,
        "queue_length": 1, "queue_index": 0, "queue": [], "volume": 0.5,
    }
    for now in (1001.0, 1100.0):
        monkeypatch.setattr(time, "time", lambda now=now: now)
        bus.set_state(dict(state))
        assert bus.get_state()["playing"] is True
        assert bus.get_state()["updated_at"] > prior["set_at"]
        assert todd_stop.is_latched()
        assert todd_stop.read_latch() == prior


def test_corrupt_latch_file_fails_closed():
    """#4011: Unreadable JSON must refuse starts instead of reopening playback."""
    todd_stop.LATCH_PATH.write_text("garbage", encoding="utf-8")
    assert todd_stop.read_latch() is not None
    message = todd_stop.gate("play_now", {}, "admin/x:y", OWNER, 1001.0)
    assert isinstance(message, str) and message


def test_json_list_latch_file_fails_closed():
    """#4011: Valid JSON with the wrong shape must also refuse starts."""
    todd_stop.LATCH_PATH.write_text("[]", encoding="utf-8")
    assert todd_stop.read_latch() is not None
    message = todd_stop.gate("play_now", {}, "admin/x:y", OWNER, 1001.0)
    assert isinstance(message, str) and message


def test_atomic_latch_write_leaves_no_temporary_file(tmp_path):
    """#4011: Completed latch writes must leave no temporary JSON file."""
    todd_stop.set_latch("t", "stop it", 1000.0)
    assert todd_stop.LATCH_PATH.is_file()
    assert not (tmp_path / "todd_stop.json.tmp").exists()
