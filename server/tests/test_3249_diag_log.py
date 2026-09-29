"""#3249: playback diagnostics survive a server restart.

On 2026-09-26 and 09-27 both rides halted ~6 s before the last song ended, and
nobody could say why: the client log and the command registry live in memory,
and restarts on 09-27 04:20Z and 09-28 01:02Z wiped them. These tests pin the
on-disk diag log: every client-log entry, each command's queued / delivered /
ack, and now-playing changes (not every heartbeat), size-capped and rotated.
"""

import json

from audiplex import playback_bus
from audiplex.playback_bus import PlaybackBus, read_diag_history


def _lines():
    path = playback_bus.DIAG_LOG_PATH
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def test_command_lifecycle_and_client_log_reach_disk(client):
    cmd = client.post("/api/playback/command",
                      json={"type": "play_now", "payload": {"track_ids": list(range(1, 50))}}).json()
    got = client.get("/api/playback/command/next").json()
    assert got["id"] == cmd["id"]
    client.post(f"/api/playback/command/{cmd['id']}/ack", json={"status": "ok", "detail": ""})
    client.post("/api/playback/client-log",
                json={"level": "error", "event": "player_error", "message": "404"})

    kinds = [l["kind"] for l in _lines()]
    assert kinds[:3] == ["cmd_queued", "cmd_delivered", "cmd_ack"]
    assert "client_log" in kinds
    queued = _lines()[0]
    # A long queue is summarized, not dumped.
    assert queued["payload"]["track_count"] == 49
    assert len(queued["payload"]["track_ids"]) == 5


def test_state_logged_on_change_not_on_heartbeat():
    bus = PlaybackBus(seq_start=0)
    s = {"playing": True, "track": {"id": 7}, "position_ms": 1000,
         "duration_ms": 250000, "queue_index": 3, "queue_length": 10}
    bus.set_state(s)
    bus.set_state({**s, "position_ms": 30000})     # heartbeat: same key
    bus.set_state({**s, "playing": False, "position_ms": 244000})  # the halt
    states = [l for l in _lines() if l["kind"] == "state"]
    assert [x["playing"] for x in states] == [True, False]
    assert states[-1]["position_ms"] == 244000
    assert states[-1]["track_id"] == 7


def test_diag_route_filters_by_kind(client):
    client.post("/api/playback/client-log", json={"event": "e1"})
    client.post("/api/playback/command", json={"type": "skip", "payload": {}})
    only = client.get("/api/playback/diag-history", params={"kind": "client_log"}).json()
    assert [x["event"] for x in only] == ["e1"]
    assert len(client.get("/api/playback/diag-history").json()) == 2


def test_rotation_caps_size(monkeypatch):
    monkeypatch.setattr(playback_bus, "DIAG_LOG_MAX_BYTES", 400)
    for i in range(60):
        playback_bus._append_diag("client_log", {"event": f"e{i}", "pad": "x" * 40})
    path = playback_bus.DIAG_LOG_PATH
    rotated = sorted(p.name for p in path.parent.glob(path.name + ".*"))
    assert rotated == [path.name + ".1", path.name + ".2", path.name + ".3"]
    assert path.stat().st_size < 400 + 200
    # Newest entry is in the live file; the oldest has rotated away.
    events = [r["event"] for r in read_diag_history(0)]
    assert events[-1] == "e59"
    assert "e0" not in events


def test_disk_failure_never_raises(monkeypatch, tmp_path):
    # A directory where the file should be: open() fails, the call must not.
    bad = tmp_path / "is_a_dir"
    bad.mkdir()
    monkeypatch.setattr(playback_bus, "DIAG_LOG_PATH", bad)
    playback_bus._append_diag("client_log", {"event": "x"})
    assert read_diag_history() == []
