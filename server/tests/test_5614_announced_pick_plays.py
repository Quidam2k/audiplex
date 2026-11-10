"""#5614: the song the intro names is the song that plays.

An announce-first refusal holds the pool's picks; the retry inside ANNOUNCE_WINDOW_S
plays those, not a re-rolled set (11/09: the intro named one song, another played).
"""
import asyncio
import sys
import time  # #5476
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402


@pytest.fixture(autouse=True)
def reset_held_picks(monkeypatch):
    monkeypatch.setattr(mcp_server, "_HELD_PICKS", {})


@pytest.fixture
def pool(monkeypatch):
    fake = SimpleNamespace(
        gate_closed=True,
        refusal=mcp_server.ANNOUNCE_REFUSAL,
        posts=[],
        trimmed=[],
    )
    pick_sequence = iter([[1, 11], [2, 12]])

    async def resolve_lanes(sources, *, budget_s, timed_out):
        return {"a": [1, 2, 3], "b": [11, 12, 13]}, []

    async def get(path):
        if path == "/api/playback/state":
            if fake.trimmed and not fake.gate_closed:  # #5476: the phone now plays the first sent pick
                return {"playing": True, "queue": [], "track": {"id": fake.trimmed[-1][0]},
                        "updated_at": time.time() + 1}
            return {"playing": False, "queue": [], "track": {}}
        return {}

    async def post(path, body):
        fake.posts.append((path, body))
        if path == "/api/playback/pool":
            return {"initial_picks": next(pick_sequence)}
        return {}

    async def trim_to_pool(picks, state):
        fake.trimmed.append(list(picks))
        return " " + fake.refusal if fake.gate_closed else " RESULT sent=2 ok"

    async def first_up_notes(track_id):
        return f" First up: {track_id}."

    monkeypatch.setattr(mcp_server, "_resolve_lanes", resolve_lanes)
    monkeypatch.setattr(mcp_server, "_counts", lambda lanes: "a=3 b=3")
    monkeypatch.setattr(mcp_server, "_get", get)
    monkeypatch.setattr(mcp_server, "_post", post)
    monkeypatch.setattr(mcp_server, "_trim_to_pool", trim_to_pool)
    monkeypatch.setattr(mcp_server, "_first_up_notes", first_up_notes)
    monkeypatch.setattr(mcp_server, "_mute_hold", lambda cmd: None)  # never the live mute
    return fake


def set_pool():
    return asyncio.run(
        mcp_server.dj_pool_set(
            sources=[
                {"kind": "playlist", "query": "a", "label": "a"},
                {"kind": "playlist", "query": "b", "label": "b"},
            ]
        )
    )


def forget_posts(pool):
    return [
        body
        for path, body in pool.posts
        if path == "/api/playback/pool/forget"
    ]


def test_refusal_holds_picks_and_names_first_up(pool):
    """#5614: Announce refusal holds picks and names the first song."""
    output = set_pool()

    assert "REFUSED (announce first)" in output
    assert "First up: 1." in output
    assert mcp_server._HELD_PICKS["picks"] == [1, 11]
    assert forget_posts(pool) == []


def test_retry_plays_the_announced_song(pool):
    """#5614: Retry plays the announced song and forgets replacement picks."""
    set_pool()
    pool.gate_closed = False

    output = set_pool()

    assert pool.trimmed == [[1, 11], [1, 11]]
    assert forget_posts(pool) == [{"track_ids": [2, 12]}]
    assert "First up: 1." in output
    assert mcp_server._HELD_PICKS == {}


def test_window_expired_rolls_fresh(pool):
    """#5614: Retry after the announcement window uses fresh picks."""
    set_pool()
    mcp_server._HELD_PICKS["at"] -= mcp_server.ANNOUNCE_WINDOW_S + 1
    pool.gate_closed = False

    output = set_pool()

    assert pool.trimmed == [[1, 11], [2, 12]]
    assert forget_posts(pool) == []
    assert "First up: 2." in output
    assert mcp_server._HELD_PICKS == {}


def test_different_pool_is_not_reused(pool, monkeypatch):
    """#5614: Different resolved lanes cannot reuse another pool's picks."""
    set_pool()

    async def different_lanes(sources, *, budget_s, timed_out):
        return {"a": [2, 3, 4], "b": [12, 13, 14]}, []

    monkeypatch.setattr(mcp_server, "_resolve_lanes", different_lanes)
    pool.gate_closed = False

    output = set_pool()

    assert pool.trimmed == [[1, 11], [2, 12]]
    assert forget_posts(pool) == []
    assert "First up: 2." in output
    assert mcp_server._HELD_PICKS == {}


def test_other_refusal_forgets(pool):
    """#5614: A talking hold forgets picks without announcing a song."""
    pool.refusal = "HELD (todd_talking) wait"

    output = set_pool()

    assert "HELD (todd_talking)" in output
    assert pool.trimmed == [[1, 11]]
    assert forget_posts(pool) == [{"track_ids": [1, 11]}]
    assert mcp_server._HELD_PICKS == {}
    assert "First up" not in output


def test_held_picks_for():
    """#5614: Held picks require a matching key and an unexpired window."""
    assert mcp_server._held_picks_for("pool-a", now=1000) is None

    mcp_server._HELD_PICKS.update(key="pool-a", picks=[1, 11], at=1000)
    deadline = 1000 + mcp_server.ANNOUNCE_WINDOW_S

    assert mcp_server._held_picks_for("pool-a", now=1000) == [1, 11]
    assert mcp_server._held_picks_for("pool-a", now=deadline) == [1, 11]
    assert mcp_server._held_picks_for("pool-b", now=1000) is None
    assert mcp_server._held_picks_for("pool-a", now=deadline + 1) is None
