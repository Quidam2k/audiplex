"""Ride fallback and MCP tool deadlines (#3924)."""

import asyncio
import inspect
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from audiplex import playback_bus, ride_fallback
from audiplex.database import _migrate_create_dj_mix_specs
from audiplex.models import Album, Artist, Track
from audiplex.playback_bus import bus
from audiplex.routers import playback

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import buckets  # noqa: E402


def _track(db, title, folder="/music/ride/lp"):
    """A track in its own album per folder, so folder sources can tell them apart."""
    art = db.query(Artist).filter(Artist.name == "Band").first()
    if art is None:
        art = Artist(name="Band")
        db.add(art)
        db.flush()
    alb = db.query(Album).filter(Album.folder_path == folder).first()
    if alb is None:
        alb = Album(title=folder.rsplit("/", 1)[-1], artist_id=art.id, genre="Rock",
                    folder_path=folder)
        db.add(alb)
        db.flush()
    track = Track(
        title=title, album_id=alb.id, artist_id=art.id, disc_number=1,
        track_number=db.query(Track).count() + 1, duration_seconds=200.0,
        file_path=f"{folder}/{title}.mp3",
    )
    db.add(track)
    db.commit()
    return track.id


def _commands(kind):
    return [rec for rec in bus._commands.values() if rec.type == kind]


def _state(tid):
    return {
        "playing": True, "track": {"id": tid, "title": "x"},
        "queue_index": 0, "queue": [{"index": 0, "id": tid}],
    }


@pytest.fixture
def pool_db(db_session, monkeypatch):
    """Route the pool's background session to the test DB."""
    class _NoClose:
        def __init__(self, session):
            self._s = session

        def __getattr__(self, key):
            return getattr(self._s, key)

        def close(self):
            pass

    monkeypatch.setattr(playback_bus, "_pool_session",
                        lambda: _NoClose(db_session))
    _migrate_create_dj_mix_specs(db_session.get_bind())
    bus.reset()
    try:
        yield db_session
    finally:
        bus.reset()


@pytest.fixture
def ride(pool_db, tmp_path, monkeypatch):
    monkeypatch.setenv("DJ_BUCKETS_DB", str(tmp_path / "buckets.db"))
    monkeypatch.setattr(playback.stop_controller, "latch_active",
                        lambda now: False)
    monkeypatch.setattr(playback.stop_controller, "latch", None)
    monkeypatch.setattr(ride_fallback, "last", {})
    # duration_seconds=0 still gives the route's minimum one-second delay.
    monkeypatch.setattr(ride_fallback, "START_GAP_S", 0)

    # #7190: one source of each kind todd-ride-mix uses
    from audiplex.routers import music
    monkeypatch.setattr(music, "get_music_roots", lambda: ["/music"])
    loose = [_track(pool_db, f"loose{i}", "/music/ride") for i in range(3)]
    fast = [_track(pool_db, f"fast{i}", "/music/ride/faster/lp") for i in range(3)]
    road = [_track(pool_db, f"road{i}") for i in range(3)]
    buckets.save_bucket("on the road", tracks=road)
    sources = [
        {"kind": "folder", "query": "/music/ride", "recursive": False, "label": "loose"},
        {"kind": "folder_match", "query": "faster", "label": "faster"},
        {"kind": "bucket", "query": "on the road", "label": "road"},
        {"kind": "genre", "query": "jazz", "label": "unsupported"},
    ]
    pool_db.execute(
        text("INSERT INTO dj_mix_specs (name, sources_json) "
             "VALUES (:name, :sources)"),
        {"name": "todd-ride-mix", "sources": json.dumps(sources)},
    )
    pool_db.commit()

    pending = []
    monkeypatch.setattr(ride_fallback, "schedule", pending.append)
    try:
        yield {"loose": set(loose), "faster": set(fast), "road": set(road),
               "pending": pending}
    finally:
        for coro in pending:
            coro.close()


@pytest.fixture
def mcp_server(monkeypatch):
    # Avoid the import-time fallback to the real .dj_token file.
    monkeypatch.setenv("AUDIPLEX_TOKEN", "test-token")
    from audiplex_mcp import server as mcp_server

    return mcp_server


def test_idle_announces_then_starts_music_from_every_source(client, ride):
    response = client.post(
        "/api/playback/ride-fallback",
        json={"clip_id": 5, "duration_seconds": 0},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    # #7190: folder + folder_match + bucket each get a lane; an unsupported kind is skipped, named
    assert body["lanes"] == {"loose": 3, "faster": 3, "road": 3}
    assert len(body["skipped"]) == 1 and body["skipped"][0].startswith("unsupported:")
    announces = _commands("announce")
    assert len(announces) == 1
    assert announces[0].payload["mode"] == "now"
    assert announces[0].payload["clip_id"] == 5
    assert not _commands("play_now")
    assert len(ride["pending"]) == 1

    result = asyncio.run(ride["pending"].pop())
    assert result["started"] is True
    picks = _commands("play_now")[0].payload["track_ids"]
    for rec in _commands("queue"):
        picks += rec.payload["track_ids"]
    lane_of = {t: lane for lane in ("loose", "faster", "road") for t in ride[lane]}
    assert set(picks) <= set(lane_of)
    # even round-robin: the first three picks are one from each lane
    assert {lane_of[t] for t in picks[:3]} == {"loose", "faster", "road"}

    status = client.get("/api/playback/ride-fallback")
    assert status.status_code == 200
    assert status.json()["started"] is True
    assert status.json()["phase"] == "music"


def test_already_playing_does_nothing(client, ride):
    bus.set_state(_state(next(iter(ride["road"]))))
    response = client.post("/api/playback/ride-fallback", json={"clip_id": 5})
    assert response.status_code == 200
    assert response.json() == {"started": False, "reason": "already playing"}
    assert not bus._commands
    assert not ride["pending"]


def test_spec_with_no_resolvable_source_is_refused(client, ride, pool_db):
    pool_db.execute(
        text("UPDATE dj_mix_specs SET sources_json = :sources WHERE name = :name"),
        {"name": "todd-ride-mix", "sources": json.dumps([
            {"kind": "folder_match", "query": "no-such-folder", "label": "nothing"},
        ])},
    )
    pool_db.commit()
    response = client.post("/api/playback/ride-fallback", json={"clip_id": 5})
    assert response.status_code == 409
    assert response.json()["started"] is False
    assert "no ride source" in response.json()["reason"]
    assert "nothing: no tracks" in response.json()["reason"]
    assert not bus._commands
    assert not ride["pending"]


def test_stop_latch_is_respected(client, ride, monkeypatch):
    monkeypatch.setattr(playback.stop_controller, "latch_active",
                        lambda now: True)
    monkeypatch.setattr(playback.stop_controller, "latch",
                        {"reason": "test stop"})
    response = client.post("/api/playback/ride-fallback", json={"clip_id": 5})
    assert response.status_code == 409
    assert response.json()["started"] is False
    assert response.json()["reason"] == "a stop is latched: test stop"
    assert not bus._commands
    assert not ride["pending"]


def test_music_started_during_announce_cancels_fallback(client, ride):
    response = client.post(
        "/api/playback/ride-fallback",
        json={"clip_id": 5, "duration_seconds": 0},
    )
    assert response.status_code == 200
    assert response.json()["started"] is True
    assert len(ride["pending"]) == 1

    bus.set_state(_state(next(iter(ride["road"]))))
    result = asyncio.run(ride["pending"].pop())
    assert result == {
        "started": False, "reason": "music started during the announce",
    }
    assert not _commands("play_now")
    assert [rec.type for rec in bus._commands.values()] == ["announce"]
    status = client.get("/api/playback/ride-fallback").json()
    assert status["started"] is False
    assert status["phase"] == "music"


def test_announce_input_is_required(client, ride):
    response = client.post("/api/playback/ride-fallback", json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "clip_id or say required"
    assert not bus._commands
    assert not ride["pending"]


def test_deadline_timeout_fast_result_signature_and_opt_out(mcp_server, monkeypatch):
    monkeypatch.setattr(mcp_server, "TOOL_DEADLINE_S", 0.05)

    async def slow(value: str = "done") -> str:
        await asyncio.sleep(1)
        return value

    async def fast(value: int = 7) -> int:
        return value

    guarded = mcp_server._with_deadline(slow)
    assert asyncio.run(guarded()).startswith("TIMED OUT")
    assert inspect.signature(guarded) == inspect.signature(slow)

    guarded_fast = mcp_server._with_deadline(fast)
    assert asyncio.run(guarded_fast(42)) == 42
    assert inspect.signature(guarded_fast) == inspect.signature(fast)

    async def dj_ingest():
        return "unused"

    assert mcp_server.TOOL_DEADLINES[dj_ingest.__name__] is None
    assert mcp_server._with_deadline(dj_ingest) is dj_ingest


def test_registered_async_tools_have_deadline_wrappers(mcp_server):
    tools = mcp_server.mcp._tool_manager._tools
    assert tools
    checked = []
    for tool in tools.values():
        fn = tool.fn
        if fn.__name__ == "dj_ingest" or not inspect.iscoroutinefunction(fn):
            continue
        assert hasattr(fn, "__wrapped__"), fn.__name__
        checked.append(fn.__name__)
    assert checked


def test_resolve_budget_skips_instead_of_waiting(ride, pool_db):
    lanes, skipped = ride_fallback.ride_lanes(pool_db, budget_s=-1)  # #7190: budget already spent
    assert lanes == {}
    assert all(s.endswith(": out of time") for s in skipped) and len(skipped) == 4
