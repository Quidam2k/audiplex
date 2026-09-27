"""DJ-bridge watcher (#2858): BridgeCounter decides which track changes fire a bridge.

Pure logic - no network, no subprocess.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import audiplex_mcp.dj_bridge_watcher as dbw  # noqa: E402
from audiplex_mcp.dj_bridge_watcher import BridgeCounter  # noqa: E402

SETTINGS_ON = {"on": True, "every_min": 2, "every_max": 3}
SETTINGS_OFF = {"on": False, "every_min": 2, "every_max": 3}


def make_state(track_id, title, artist, playing=True, position_ms=0,
                duration_ms=200000, queue=None, queue_index=0):
    track = {"id": track_id, "title": title, "artist": artist} if track_id is not None else None
    return {
        "playing": playing,
        "track": track,
        "position_ms": position_ms,
        "duration_ms": duration_ms,
        "queue_length": len(queue or []),
        "queue_index": queue_index,
        "queue": queue or [],
    }


def test_first_track_initializes_only():
    c = BridgeCounter()
    result = c.observe(make_state(1, "A", "Artist A"), SETTINGS_ON, 1000.0)
    assert result is None
    assert c.counter == 0
    assert c.last_music == {"id": 1, "title": "A", "artist": "Artist A"}
    assert c.prev_music is None
    assert c.last_skip_reason == "init"


def test_counts_transitions_and_fires_at_target(monkeypatch):
    monkeypatch.setattr(dbw.random, "randint", lambda a, b: 2)
    c = BridgeCounter()
    c.observe(make_state(1, "A", "AA"), SETTINGS_ON, 1000.0)  # init

    r1 = c.observe(make_state(2, "B", "BB"), SETTINGS_ON, 1001.0)  # transition 1
    assert r1 is None
    assert c.counter == 1

    r2 = c.observe(make_state(3, "C", "CC"), SETTINGS_ON, 1002.0)  # transition 2 -> fire
    assert r2 is not None
    assert r2["prev"] == {"title": "B", "artist": "BB"}
    assert r2["now"]["title"] == "C"
    assert r2["source"] == "audiplex"
    assert c.counter == 0


def test_ignores_nonmusic_clip_without_resetting_prev(monkeypatch):
    monkeypatch.setattr(dbw.random, "randint", lambda a, b: 5)
    c = BridgeCounter()
    c.observe(make_state(1, "A", "AA"), SETTINGS_ON, 1000.0)  # init
    c.observe(make_state(2, "B", "BB"), SETTINGS_ON, 1001.0)  # transition, counter=1

    clip = make_state(-1, "DJ Clip", "Host")
    r = c.observe(clip, SETTINGS_ON, 1002.0)
    assert r is None
    assert c.last_skip_reason == "not_music"
    assert c.last_music == {"id": 2, "title": "B", "artist": "BB"}
    assert c.prev_music == {"id": 1, "title": "A", "artist": "AA"}
    assert c.counter == 1  # untouched by the clip

    # next real transition compares against B, not the clip
    r2 = c.observe(make_state(3, "C", "CC"), SETTINGS_ON, 1003.0)
    assert r2 is None  # target is 5, only counter==2 now
    assert c.prev_music == {"id": 2, "title": "B", "artist": "BB"}
    assert c.counter == 2


def test_same_id_pause_resume_no_count(monkeypatch):
    monkeypatch.setattr(dbw.random, "randint", lambda a, b: 5)
    c = BridgeCounter()
    c.observe(make_state(1, "A", "AA"), SETTINGS_ON, 1000.0)
    c.observe(make_state(2, "B", "BB"), SETTINGS_ON, 1001.0)  # counter=1

    paused = make_state(2, "B", "BB", playing=False)
    assert c.observe(paused, SETTINGS_ON, 1002.0) is None
    assert c.counter == 1

    resumed = make_state(2, "B", "BB", playing=True, position_ms=5000)
    assert c.observe(resumed, SETTINGS_ON, 1003.0) is None
    assert c.counter == 1


def test_off_resets_counter(monkeypatch):
    monkeypatch.setattr(dbw.random, "randint", lambda a, b: 5)
    c = BridgeCounter()
    c.observe(make_state(1, "A", "AA"), SETTINGS_ON, 1000.0)
    c.observe(make_state(2, "B", "BB"), SETTINGS_ON, 1001.0)  # counter=1

    r = c.observe(make_state(3, "C", "CC"), SETTINGS_OFF, 1002.0)
    assert r is None
    assert c.counter == 0
    assert c.last_skip_reason == "off"


def test_stale_position_skips_firing(monkeypatch):
    monkeypatch.setattr(dbw.random, "randint", lambda a, b: 1)  # fires on first transition
    c = BridgeCounter()
    c.observe(make_state(1, "A", "AA"), SETTINGS_ON, 1000.0)  # init

    stale = make_state(2, "B", "BB", position_ms=40000)  # position_s = 40 > 25
    r = c.observe(stale, SETTINGS_ON, 1001.0)
    assert r is None
    assert c.counter == 0
    assert c.last_skip_reason == "stale"


def test_payload_next_is_queue_index_plus_one(monkeypatch):
    monkeypatch.setattr(dbw.random, "randint", lambda a, b: 1)  # fires on first transition
    c = BridgeCounter()
    queue = [
        {"index": 0, "id": 1, "title": "A", "artist": "AA"},
        {"index": 1, "id": 2, "title": "B", "artist": "BB"},
        {"index": 2, "id": 3, "title": "C", "artist": "CC"},
    ]
    c.observe(make_state(1, "A", "AA", queue=queue, queue_index=0), SETTINGS_ON, 1000.0)
    r = c.observe(
        make_state(2, "B", "BB", queue=queue, queue_index=1, position_ms=1000),
        SETTINGS_ON, 1001.0,
    )
    assert r is not None
    assert r["next"] == {"title": "C", "artist": "CC"}
