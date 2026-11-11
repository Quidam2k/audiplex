"""#5648 P3 watcher: the 'prepare' hook at 15 s into an armed song, and the T-20 'outro'
hook as a backstop that is skipped when the server already holds a prepared break.
"""
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import audiplex_mcp.dj_bridge_watcher as dbw  # noqa: E402
from audiplex_mcp.dj_bridge_watcher import BridgeCounter  # noqa: E402

SETTINGS_ON = {"on": True, "every_min": 2, "every_max": 3}


def make_state(track_id, title, artist, position_ms=0, duration_ms=200000):
    return {
        "playing": True,
        "track": {"id": track_id, "title": title, "artist": artist},
        "position_ms": position_ms,
        "duration_ms": duration_ms,
        "queue_length": 0,
        "queue_index": 0,
        "queue": [],
    }


def _armed(monkeypatch):
    monkeypatch.setattr(dbw.random, "randint", lambda a, b: 1)
    c = BridgeCounter()
    c.observe(make_state(1, "A", "AA"), SETTINGS_ON, 1000.0)
    assert c.observe(make_state(2, "B", "BB"), SETTINGS_ON, 1001.0) is None
    assert c.armed["id"] == 2
    return c


def test_prepare_fires_15_s_into_the_armed_song(monkeypatch):
    c = _armed(monkeypatch)
    assert c.observe(make_state(2, "B", "BB", position_ms=14000), SETTINGS_ON, 1015.0) is None
    p = c.observe(make_state(2, "B", "BB", position_ms=15000), SETTINGS_ON, 1016.0)
    assert p is not None and p["mode"] == "prepare"
    assert p["now"]["id"] == 2 and p["prev"]["id"] == 1 and p["source"] == "audiplex"
    assert c.armed is not None  # arming survives: the T-20 outro still fires
    # once per armed song
    assert c.observe(make_state(2, "B", "BB", position_ms=30000), SETTINGS_ON, 1031.0) is None


def test_prepare_then_outro_still_fires_at_T_minus_20(monkeypatch):
    c = _armed(monkeypatch)
    assert c.observe(make_state(2, "B", "BB", position_ms=15000), SETTINGS_ON, 1016.0)["mode"] == "prepare"
    out = c.observe(make_state(2, "B", "BB", position_ms=182000), SETTINGS_ON, 1183.0)
    assert out is not None and out["mode"] == "outro"


def test_no_prepare_when_the_song_is_already_inside_the_outro_window(monkeypatch):
    c = _armed(monkeypatch)
    # first sight of the armed song only 18 s from its end: the outro, not a prepare
    out = c.observe(make_state(2, "B", "BB", position_ms=182000), SETTINGS_ON, 1183.0)
    assert out["mode"] == "outro"


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _outro_payload():
    return {"mode": "outro", "now": {"id": 2, "title": "B"}, "prev": None, "source": "audiplex"}


def test_backstop_skipped_when_a_prepared_entry_exists(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"exists": True, "entry": {"id": "p1", "status": "ready"}})

    with _client(handler) as client:
        assert dbw.outro_backstop(_outro_payload(), client, "http://x", "") is None
    assert "/api/playback/pool/prepared" in seen["url"] and "track_id=2" in seen["url"]


def test_backstop_fires_when_no_prepared_entry_exists():
    def handler(request):
        return httpx.Response(200, json={"exists": False, "entry": None})

    with _client(handler) as client:
        p = _outro_payload()
        assert dbw.outro_backstop(p, client, "http://x", "") is p


def test_backstop_fires_when_the_lookup_fails():
    def handler(request):
        return httpx.Response(500, json={"detail": "boom"})

    with _client(handler) as client:
        p = _outro_payload()
        assert dbw.outro_backstop(p, client, "http://x", "") is p  # a break is never lost


def test_backstop_leaves_prepare_and_intro_payloads_alone():
    def handler(request):  # must not be asked at all
        raise AssertionError("no lookup for non-outro payloads")

    with _client(handler) as client:
        for mode in ("prepare", "intro"):
            p = {"mode": mode, "now": {"id": 2, "title": "B"}}
            assert dbw.outro_backstop(p, client, "http://x", "") is p
        assert dbw.outro_backstop(None, client, "http://x", "") is None
