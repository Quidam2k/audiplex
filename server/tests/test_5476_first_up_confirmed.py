"""#5476: dj_pool_set's "First up" names the song the phone reports playing.

11/10 ride (Todd's third report): the reply said "First up: Sympathy For The Devil"
(the pool's picks[0]) while the server's five-star guard had sent Tumbling
Tumbleweeds first, and that is what played.
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402

SYMPATHY, TUMBLEWEEDS = 1722, 4932


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(mcp_server, "FIRST_UP_CONFIRM_S", 0.3)
    monkeypatch.setattr(mcp_server, "FIRST_UP_POLL_S", 0.05)
    monkeypatch.setattr(mcp_server, "_HELD_PICKS", {})

    async def notes(track_id):
        return f" First up: {track_id}."

    monkeypatch.setattr(mcp_server, "_first_up_notes", notes)


def states(monkeypatch, *seq):
    """_get returns these now-playing states in turn (the last one repeats)."""
    seq = list(seq)
    calls = []

    async def get(path):
        assert path == "/api/playback/state"
        calls.append(path)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(mcp_server, "_get", get)
    return calls


def playing(track_id, at):
    return {"playing": True, "track": {"id": track_id}, "updated_at": at}


def confirm(picks, acked_at):
    return asyncio.run(mcp_server._confirmed_first_up(picks, acked_at))


def test_names_the_track_the_phone_plays_not_picks_0(monkeypatch):
    t0 = time.time()
    states(monkeypatch, playing(TUMBLEWEEDS, t0 + 0.1))
    assert confirm([SYMPATHY, TUMBLEWEEDS], t0) == f" First up: {TUMBLEWEEDS}."


def test_waits_for_a_state_newer_than_the_ack(monkeypatch):
    t0 = time.time()
    calls = states(monkeypatch, playing(SYMPATHY, t0 - 5), playing(TUMBLEWEEDS, t0 + 0.1))
    assert confirm([SYMPATHY, TUMBLEWEEDS], t0) == f" First up: {TUMBLEWEEDS}."
    assert len(calls) == 2


def test_stale_state_is_never_named(monkeypatch):
    t0 = time.time()
    states(monkeypatch, playing(SYMPATHY, t0 - 5))
    start = time.monotonic()
    assert confirm([SYMPATHY, TUMBLEWEEDS], t0) == mcp_server.FIRST_UP_UNCONFIRMED
    assert time.monotonic() - start < 1.0  # bounded by FIRST_UP_CONFIRM_S


def test_fresh_state_of_a_track_not_sent_is_never_named(monkeypatch):
    t0 = time.time()
    states(monkeypatch, playing(999, t0 + 0.1))  # the old song, reported late
    assert confirm([SYMPATHY, TUMBLEWEEDS], t0) == mcp_server.FIRST_UP_UNCONFIRMED


def test_paused_or_unreadable_state_is_unconfirmed(monkeypatch):
    t0 = time.time()
    states(monkeypatch, {"playing": False, "track": {"id": SYMPATHY}, "updated_at": t0 + 1})
    assert confirm([SYMPATHY], t0) == mcp_server.FIRST_UP_UNCONFIRMED

    async def boom(path):
        raise RuntimeError("server down")

    monkeypatch.setattr(mcp_server, "_get", boom)
    assert confirm([SYMPATHY], t0) == mcp_server.FIRST_UP_UNCONFIRMED


def test_unconfirmed_wording_names_no_song():
    assert str(SYMPATHY) not in mcp_server.FIRST_UP_UNCONFIRMED
    assert "not confirmed" in mcp_server.FIRST_UP_UNCONFIRMED


def test_dj_pool_set_start_from_silence_names_what_the_phone_plays(monkeypatch):
    sent = []

    async def get(path):
        if path == "/api/playback/state":
            if sent:  # the guard put the five-star first; the phone plays it
                return playing(TUMBLEWEEDS, time.time() + 1)
            return {"playing": False, "queue": [], "track": {}}
        return {}

    async def post(path, body):
        if path == "/api/playback/pool":
            return {"initial_picks": [SYMPATHY, TUMBLEWEEDS]}
        return {}

    async def trim_to_pool(picks, state):
        sent.append([TUMBLEWEEDS, SYMPATHY])
        return " RESULT sent=2 ok"

    async def resolve_lanes(sources, *, budget_s, timed_out):
        return {"a": [SYMPATHY], "b": [TUMBLEWEEDS]}, []

    monkeypatch.setattr(mcp_server, "_get", get)
    monkeypatch.setattr(mcp_server, "_post", post)
    monkeypatch.setattr(mcp_server, "_trim_to_pool", trim_to_pool)
    monkeypatch.setattr(mcp_server, "_resolve_lanes", resolve_lanes)
    monkeypatch.setattr(mcp_server, "_counts", lambda lanes: "a=1 b=1")
    monkeypatch.setattr(mcp_server, "_mute_hold", lambda cmd: None)
    out = asyncio.run(mcp_server.dj_pool_set(sources=[
        {"kind": "playlist", "query": "a", "label": "a"},
        {"kind": "playlist", "query": "b", "label": "b"},
    ]))
    assert f"First up: {TUMBLEWEEDS}." in out
    assert f"First up: {SYMPATHY}." not in out


def test_a_slow_state_read_never_stretches_the_budget(monkeypatch):
    t0 = time.time()

    async def slow_get(path):
        await asyncio.sleep(2)
        return playing(SYMPATHY, time.time())

    monkeypatch.setattr(mcp_server, "_get", slow_get)
    start = time.monotonic()
    assert confirm([SYMPATHY], t0) == mcp_server.FIRST_UP_UNCONFIRMED
    assert time.monotonic() - start < 1.0


def test_the_report_right_after_the_ack_counts(monkeypatch):
    # The phone posts now-playing straight after its ack, before the MCP sees the ack.
    sent_at = time.time() - 0.5
    states(monkeypatch, playing(TUMBLEWEEDS, sent_at + 0.1))
    assert confirm([SYMPATHY, TUMBLEWEEDS], sent_at) == f" First up: {TUMBLEWEEDS}."
