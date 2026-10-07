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

    r2 = c.observe(make_state(3, "C", "CC"), SETTINGS_ON, 1002.0)  # transition 2 -> arm (#5986)
    assert r2 is None
    assert c.last_skip_reason == "armed"
    # mid-song: nothing
    assert c.observe(make_state(3, "C", "CC", position_ms=100000), SETTINGS_ON, 1100.0) is None
    # 18 s before the end: outro fires (#5986)
    r3 = c.observe(make_state(3, "C", "CC", position_ms=182000), SETTINGS_ON, 1200.0)
    assert r3 is not None
    assert r3["mode"] == "outro"
    assert r3["prev"] == {"id": 2, "title": "B", "artist": "BB"}  # #3912 id
    assert r3["now"]["title"] == "C"
    assert r3["source"] == "audiplex"
    assert c.counter == 0 and c.armed is None


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
    assert c.observe(make_state(2, "B", "BB", queue=queue, queue_index=1, position_ms=1000),
                     SETTINGS_ON, 1001.0) is None  # armed
    r = c.observe(
        make_state(2, "B", "BB", queue=queue, queue_index=1, position_ms=185000),
        SETTINGS_ON, 1180.0,
    )
    assert r is not None
    assert r["next"] == {"id": 3, "title": "C", "artist": "CC"}
    assert r["after"] is None  # #6005 end of queue: no third beat


def test_payload_after_is_queue_index_plus_two(monkeypatch):  # #6005
    queue = [{"index": i, "id": i + 1, "title": t, "artist": t * 2} for i, t in enumerate("ABCD")]
    state = {"queue_index": 1, "queue": queue}
    assert BridgeCounter._next_item(state) == {"id": 3, "title": "C", "artist": "CC"}
    assert BridgeCounter._next_item(state, 2) == {"id": 4, "title": "D", "artist": "DD"}


# --- #5986 outro timing + cadence ------------------------------------------

def _armed(monkeypatch, target=1):
    monkeypatch.setattr(dbw.random, "randint", lambda a, b: target)
    c = BridgeCounter()
    c.observe(make_state(1, "A", "AA"), SETTINGS_ON, 1000.0)
    assert c.observe(make_state(2, "B", "BB"), SETTINGS_ON, 1001.0) is None
    assert c.armed["id"] == 2
    return c


def test_outro_extrapolates_from_updated_at(monkeypatch):  # #5986
    c = _armed(monkeypatch)
    st = make_state(2, "B", "BB", position_ms=150000)  # 50 s left as posted...
    st["updated_at"] = 1000.0
    assert c.observe(st, SETTINGS_ON, 1035.0)["mode"] == "outro"  # ...but 35 s have passed


def test_skipped_armed_song_fires_intro_on_next(monkeypatch):  # #5986
    c = _armed(monkeypatch)
    r = c.observe(make_state(3, "C", "CC", position_ms=2000), SETTINGS_ON, 1010.0)
    assert r["mode"] == "intro"
    assert r["prev"] == {"id": 2, "title": "B", "artist": "BB"} and r["now"]["title"] == "C" # #3912
    assert c.armed is None and c.counter == 0


def test_too_close_to_end_waits_for_intro(monkeypatch):  # #5986
    c = _armed(monkeypatch)
    assert c.observe(make_state(2, "B", "BB", position_ms=197000), SETTINGS_ON, 1200.0) is None
    assert c.observe(make_state(3, "C", "CC", position_ms=1000), SETTINGS_ON, 1204.0)["mode"] == "intro"


def test_unknown_duration_falls_back_to_intro(monkeypatch):  # #5986
    c = _armed(monkeypatch)
    assert c.observe(make_state(2, "B", "BB", position_ms=500000, duration_ms=0), SETTINGS_ON, 1300.0) is None
    assert c.observe(make_state(3, "C", "CC"), SETTINGS_ON, 1301.0)["mode"] == "intro"


def test_cadence_three_songs_between_bridges(monkeypatch):  # #5986
    monkeypatch.setattr(dbw.random, "randint", lambda a, b: 3)
    c = BridgeCounter()
    fires, t = [], 1000.0
    c.observe(make_state(1, "S1", "X"), SETTINGS_ON, t)
    for tid in range(2, 12):
        t += 1
        if c.observe(make_state(tid, f"S{tid}", "X"), SETTINGS_ON, t):
            fires.append(("intro", tid))
        t += 190
        if c.observe(make_state(tid, f"S{tid}", "X", position_ms=185000), SETTINGS_ON, t):
            fires.append(("outro", tid))
    assert fires == [("outro", 4), ("outro", 7), ("outro", 10)]


def test_default_cadence_is_three():  # #5986
    assert dbw.DEFAULT_SETTINGS["every_min"] == dbw.DEFAULT_SETTINGS["every_max"] == 3


# --- #6038 real queue order + album artist ---------------------------------

def _q(*items):  # #6038 (id, title) in queue order
    return [{"index": i, "id": tid, "title": t, "artist": t * 2} for i, (tid, t) in enumerate(items)]


def test_next_and_after_skip_dj_clips():  # #6038 a clip between two songs is not 'coming up next'
    state = {"queue_index": 0, "queue": _q((1, "A"), (-7, "clip"), (2, "B"), (0, "stream"), (3, "C"))}
    assert BridgeCounter._next_item(state) == {"id": 2, "title": "B", "artist": "BB"}
    assert BridgeCounter._next_item(state, 2) == {"id": 3, "title": "C", "artist": "CC"}
    assert BridgeCounter._next_item(state, 3) is None


def test_next_follows_index_not_list_position():  # #6038
    queue = list(reversed(_q((1, "A"), (2, "B"), (3, "C"))))
    assert BridgeCounter._next_item({"queue_index": 0, "queue": queue}) == {"id": 2, "title": "B", "artist": "BB"}
    assert BridgeCounter._next_item({"queue_index": None, "queue": queue}) is None


def test_intro_prev_falls_back_to_the_queue(monkeypatch):  # #6038 watcher restarted mid-set: no remembered prev
    c = BridgeCounter()
    state = make_state(3, "C", "CC", queue=_q((1, "A"), (-7, "clip"), (3, "C"), (4, "D")), queue_index=2)
    p = c._payload("intro", state["track"], None, state, 1000.0)
    assert p["prev"] == {"id": 1, "title": "A", "artist": "AA"} and p["next"]["title"] == "D" # #3912
    remembered = c._payload("intro", state["track"], {"title": "Z", "artist": "ZZ"}, state, 1000.0)
    assert remembered["prev"] == {"id": None, "title": "Z", "artist": "ZZ"}  # what was actually heard wins #3912


class _Resp:  # #6038
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _Client:  # #6038
    def __init__(self, album):
        self.album = album

    def get(self, url, headers=None, timeout=None):
        return _Resp({"album_id": 9} if "/tracks/" in url else self.album)


def test_facts_carry_album_artist():  # #6038 Pantheon needs it to tell a folder from an album
    folder = {"title": "On Playa", "artist_name": "faster", "year": 1999, "genre": None}
    assert dbw.fetch_facts(_Client(folder), "http://x", "", 5) == {
        "album_artist": "faster", "album": "On Playa", "year": 1999}
    assert "album_artist" not in dbw.fetch_facts(_Client({"title": "Kick", "year": 1987}), "http://x", "", 5)


class _CalloutClient:  # #3912
    def __init__(self, tracks=None, fail=False):
        self.tracks, self.fail, self.params = tracks or {}, fail, None

    def get(self, url, headers=None, timeout=None, params=None):
        if "/callouts" in url:
            if self.fail:
                raise OSError("down")
            self.params = params
            return _Resp({"tracks": self.tracks})
        return _Resp({"album_id": 9} if "/tracks/" in url else {"title": "LP"})


def test_enrich_flags_each_beat_and_consumes():  # #3912
    payload = {"prev": {"id": 1}, "now": {"id": 2}, "next": {"id": 3}, "after": {"id": 4}}
    client = _CalloutClient({"1": {"callout": True, "why": "whole album: X"}, "2": {"callout": False, "why": "picks"},
                             "3": {"callout": True, "why": "you asked what this was"}})
    p = dbw.enrich_payload(payload, client, "http://x", "")
    assert client.params == {"ids": "1,2,3,4", "consume": "true"}
    assert p["prev"]["callout"] is True and p["now"]["callout"] is False and p["next"]["callout"] is True
    assert p["now"]["callout_why"] == "picks" and "callout" not in p["after"]


def test_enrich_without_callouts_keeps_old_payload(monkeypatch, tmp_path):  # #3912 server down = name every song, as before
    monkeypatch.setenv("DJ_BRIDGE_LOG", str(tmp_path / "bridge.log"))  # not the live log
    p = dbw.enrich_payload({"now": {"id": 2}, "next": {"id": 3}}, _CalloutClient(fail=True), "http://x", "")
    assert "callout" not in p["now"] and "callout" not in p["next"]
