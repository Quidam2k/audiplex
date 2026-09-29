"""#3367: a no-arg dj_sleep_start fades the current book into the brown-noise bed."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402

BOOKS = [
    {"id": 3, "title": "Some Novel"},
    {"id": 41, "title": "Brown Noise - Sleep Loop"},
]
PLAYING = {"playing": True, "track": {"id": 500, "title": "Chapter 7"}, "position_ms": 4_000_000}
START_CMDS = {"play_now", "queue", "play_next", "play_stream", "resume", "seek", "replace_upcoming"}


@pytest.fixture
def wire(monkeypatch):
    calls = []
    st = {"books": list(BOOKS), "state": PLAYING}

    async def fake_get(path):
        calls.append(("GET", path))
        if path.startswith("/api/library/books"):
            return st["books"]
        if path.startswith("/api/playback/state"):
            return st["state"]
        return []

    async def fake_post(path, body):
        return {"playable": body.get("track_ids", []), "missing": []}

    async def fake_raw(cmd, payload):
        calls.append(("CMD", cmd, payload))
        return {"id": len(calls), "pending": 1}

    async def no_gate(cmd):
        return None

    monkeypatch.delenv("AUDIPLEX_SLEEP_BED_URL", raising=False)
    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_enqueue_raw", fake_raw)
    monkeypatch.setattr(mcp_server, "_announce_gate", no_gate)
    monkeypatch.setattr(mcp_server, "_todd_talking", lambda: "quiet")
    return calls, st


def cmds(calls):
    return [(c[1], c[2]) for c in calls if c[0] == "CMD"]


def run(**kw):
    return asyncio.run(mcp_server.dj_sleep_start(**kw))


def test_no_arg_crossfades_current_book_into_default_bed(wire):
    calls, _ = wire
    run()
    sent = cmds(calls)
    assert [c for c, _ in sent] == ["bed_play", "sleep_timer"]
    bed, timer = sent[0][1], sent[1][1]
    assert bed["url"] == "/api/stream/41" and bed["volume"] == 0
    assert timer == {"minutes": 30, "fade_seconds": 120, "bed_fade_to": 0.5}


def test_no_arg_never_restarts_or_requeues_the_book(wire):
    # Karen rider (2): the timer rides on whatever is playing; the book's
    # position is never touched.
    calls, _ = wire
    run()
    assert not [c for c, _ in cmds(calls) if c in START_CMDS]


def test_todds_own_track_beats_the_generated_loop(wire):
    calls, st = wire
    st["books"].append({"id": 77, "title": "Star Ship Sleeping Quarters"})
    run()
    assert cmds(calls)[0][1]["url"] == "/api/stream/77"
    assert cmds(calls)[0][1]["title"] == "Star Ship Sleeping Quarters"


def test_under_mode_keeps_bed_at_volume_from_the_start(wire):
    calls, _ = wire
    run(bed_mode="under", bed_volume=30)
    (_, bed), (_, timer) = cmds(calls)
    assert bed["volume"] == pytest.approx(0.3)
    assert "bed_fade_to" not in timer


def test_nothing_playing_starts_just_the_bed_audibly(wire):
    calls, st = wire
    st["state"] = {"playing": False, "track": None}
    out = run()
    sent = cmds(calls)
    assert [c for c, _ in sent] == ["bed_play"]
    assert sent[0][1]["volume"] == 0.5
    assert "just the bed" in out


def test_explicit_bed_url_skips_the_lookup(wire):
    calls, _ = wire
    run(bed_url="http://x/noise.mp3")
    assert not [c for c in calls if c[0] == "GET" and c[1].startswith("/api/library")]
    assert cmds(calls)[0][1]["url"] == "http://x/noise.mp3"


def test_env_override(wire, monkeypatch):
    calls, _ = wire
    monkeypatch.setenv("AUDIPLEX_SLEEP_BED_URL", "http://y/bed.m4a")
    run()
    assert cmds(calls)[0][1]["url"] == "http://y/bed.m4a"


def test_no_bed_in_library_says_so_and_sends_nothing(wire):
    calls, st = wire
    st["books"] = [{"id": 3, "title": "Some Novel"}]
    out = run()
    assert "No sleep bed found" in out
    assert not cmds(calls)


def test_bad_mode_rejected(wire):
    calls, _ = wire
    assert "bed_mode" in run(bed_mode="loud")
    assert not cmds(calls)


def test_starting_a_new_book_still_times_it(wire):
    calls, _ = wire
    run(fade_track_ids=[9])
    assert [c for c, _ in cmds(calls)] == ["bed_play", "play_now", "sleep_timer"]


def test_sleep_timer_legacy_payload_unchanged(wire):
    calls, _ = wire
    asyncio.run(mcp_server.dj_sleep_timer(45, 60))
    assert cmds(calls) == [("sleep_timer", {"minutes": 45, "fade_seconds": 60})]
