"""DJ pool wiring (#5495): the server owns the pool; the MCP talks to it over HTTP.

Covers the bus top-up hook (append-only "queue" commands on a track change),
cue firing, recent/same-recording exclusion by the DJ owner's history, the
state-path env var, the /pool and /mix-specs routes on the get_pool()
singleton, and the MCP tools going through _delete/_patch instead of a
private DJPool of their own.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from audiplex import dj_pool, playback_bus
from audiplex.config import get_settings
from audiplex.database import _migrate_create_dj_mix_specs
from audiplex.models import Album, Artist, PlayStat, Track, User
from audiplex.playback_bus import bus

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402

MCP_SOURCE = Path(mcp_server.__file__).read_text(encoding="utf-8")


# ----- helpers -----


def _track(db, title, *, file_hash=None, artist="Band"):
    art = db.query(Artist).filter(Artist.name == artist).first()
    if art is None:
        art = Artist(name=artist)
        db.add(art)
        db.flush()
    alb = db.query(Album).filter(Album.artist_id == art.id).first()
    if alb is None:
        alb = Album(title="LP", artist_id=art.id, genre="Rock", folder_path=f"/fake/{artist}")
        db.add(alb)
        db.flush()
    t = Track(
        title=title, album_id=alb.id, artist_id=art.id, disc_number=1,
        track_number=db.query(Track).count() + 1, duration_seconds=200.0,
        file_path=f"/fake/{artist}/{title}.mp3", file_hash=file_hash,
    )
    db.add(t)
    db.commit()
    return t.id


def _owner(db):
    name = get_settings().dj_owner_username
    user = db.query(User).filter(User.username == name).first()
    if user is None:
        user = User(username=name, password_hash="x", display_name="Owner", is_admin=True)
        db.add(user)
        db.commit()
    return user.id


def _played(db, user_id, track_id, minutes_ago=5):
    db.add(PlayStat(track_id=track_id, user_id=user_id, event="complete", played_seconds=200,
                    timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))
    db.commit()


def _state(current, after=(), playing=True):
    queue = [{"index": 0, "id": current}] + [{"index": i + 1, "id": t} for i, t in enumerate(after)]
    return {"playing": playing, "track": {"id": current, "title": "x"}, "queue_index": 0, "queue": queue}


def _queued():
    return [(r["type"], r["payload"]["track_ids"]) for r in _rows() if r["type"] == "queue"]


def _rows():
    out = []
    for rec in bus._commands.values():
        out.append({"type": rec.type, "payload": rec.payload})
    return out


@pytest.fixture
def pool_db(db_session, monkeypatch):
    """Route the bus hook's DB session to the test DB (never the real one)."""
    class _NoClose:
        def __init__(self, s):
            self._s = s

        def __getattr__(self, k):
            return getattr(self._s, k)

        def close(self):
            pass

    monkeypatch.setattr(playback_bus, "_pool_session", lambda: _NoClose(db_session))
    bus.reset()
    yield db_session
    bus.reset()


def _start(lanes, ahead=3, **kw):
    pool = dj_pool.get_pool()
    pool.set_pool(7, [t for v in lanes.values() for t in v], {},
                  lanes=dj_pool.lanes_from_ids(lanes), ahead=ahead, **kw)
    return pool


# ----- bus hook -----


def test_track_change_enqueues_queue_up_to_ahead(pool_db):
    ids = [_track(pool_db, f"s{i}") for i in range(6)]
    _start({"a": ids[:3], "b": ids[3:]}, ahead=3)
    bus.set_state(_state(ids[0], after=[ids[1]]))
    queued = _queued()
    assert len(queued) == 1
    picks = queued[0][1]
    assert len(picks) == 2  # 1 already ahead, topped up to 3
    assert ids[0] not in picks and ids[1] not in picks  # never re-queue what's there
    # Only "queue" (append) commands; nothing that replaces the queue.
    assert {r["type"] for r in _rows()} == {"queue"}


def test_same_track_report_does_not_top_up_twice(pool_db):
    ids = [_track(pool_db, f"s{i}") for i in range(6)]
    _start({"a": ids}, ahead=2)
    bus.set_state(_state(ids[0]))
    bus.set_state(_state(ids[0]))  # position update, same track
    assert len(_queued()) == 1
    bus.set_state(_state(ids[1]))  # track change, nothing queued after it
    assert len(_queued()) == 2


@pytest.mark.parametrize("state", [
    _state(1, playing=False),                       # paused
    {"playing": True, "track": {"id": -1}, "queue": []},  # live stream
    _state(-5),                                     # DJ voice break
])
def test_no_topup_when_paused_stream_or_break(pool_db, state):
    ids = [_track(pool_db, f"s{i}") for i in range(4)]
    _start({"a": ids}, ahead=2)
    bus.set_state(state)
    assert _queued() == []


def test_no_topup_without_pool(pool_db):
    ids = [_track(pool_db, f"s{i}") for i in range(3)]
    bus.set_state(_state(ids[0]))
    assert _queued() == []


def test_matching_cue_play_track_goes_first_and_is_marked_done(pool_db):
    ids = [_track(pool_db, f"s{i}") for i in range(6)]
    pool = _start({"a": ids[:5]}, ahead=3)
    pool.add_cue(11, "track_start", ids[0], play_track=ids[5], say="here it comes")
    bus.set_state(_state(ids[0]))
    picks = _queued()[0][1]
    assert picks[0] == ids[5]
    assert len(picks) == 3
    assert pool.get_pending_cues() == []
    assert pool.state["pending_cues"][0]["done"] is True


def test_track_end_cue_fires_on_the_next_track(pool_db):
    ids = [_track(pool_db, f"s{i}") for i in range(6)]
    pool = _start({"a": ids[:4]}, ahead=1)
    pool.add_cue(12, "track_end", ids[0], play_track=ids[5])
    bus.set_state(_state(ids[0], after=[ids[1]]))  # full; cue not yet due
    assert _queued() == []
    bus.set_state(_state(ids[1]))  # ids[0] ended
    assert _queued()[0][1][0] == ids[5]


def test_recent_owner_plays_and_same_recording_are_excluded(pool_db):
    owner = _owner(pool_db)
    recent = _track(pool_db, "recent")
    copy_of_recent = _track(pool_db, "recent copy", file_hash="h-recent")
    pool_db.query(Track).filter(Track.id == recent).update({"file_hash": "h-recent"})
    pool_db.commit()
    twin_a = _track(pool_db, "twin", file_hash="h-twin")
    twin_b = _track(pool_db, "twin again", file_hash="h-twin")
    fresh = _track(pool_db, "fresh")
    current = _track(pool_db, "now")
    _played(pool_db, owner, recent)
    _start({"a": [recent, copy_of_recent, twin_a, twin_b, fresh]}, ahead=4)
    bus.set_state(_state(current))
    picks = _queued()[0][1]
    assert recent not in picks and copy_of_recent not in picks  # recent recording, any copy
    assert not (twin_a in picks and twin_b in picks)  # one recording, one slot
    assert fresh in picks


def test_recent_filter_uses_owner_not_user_1(pool_db):
    other = pool_db.query(User).filter(User.username == "testuser").first().id  # id 1
    _owner(pool_db)
    heard_by_other = _track(pool_db, "theirs")
    current = _track(pool_db, "now")
    _played(pool_db, other, heard_by_other)
    if other == _owner(pool_db):
        pytest.skip("owner is the test user in this config")
    _start({"a": [heard_by_other]}, ahead=1)
    bus.set_state(_state(current))
    assert _queued()[0][1] == [heard_by_other]


def test_pool_state_env_path_is_honored(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere" / "pool.json"
    monkeypatch.setenv("AUDIPLEX_DJ_POOL_STATE", str(target))
    dj_pool.reset_pool_singleton()
    pool = dj_pool.get_pool()
    assert pool.state_file == target
    pool.set_pool(1, [5], {})
    assert target.exists()
    assert dj_pool.get_pool() is pool  # singleton


# ----- routes -----


@pytest.fixture
def specs(db_engine):
    _migrate_create_dj_mix_specs(db_engine)


def test_delete_pool_reports_stopped_then_not(client):
    dj_pool.get_pool().set_pool(3, [1, 2], {})
    assert client.delete("/api/playback/pool").json() == {"stopped": True}
    assert client.delete("/api/playback/pool").json() == {"stopped": False}


def test_post_pool_with_lanes_sets_status_lanes(client, specs):
    r = client.post("/api/playback/pool", json={
        "lanes": {"faster": [1, 2, 3], "slower": [4], "gone": []},
        "balance": "even", "ahead": 5, "exclude_recent_hours": 6,
        "starvation_config": {"check_interval_picks": 3},
    })
    assert r.status_code == 200
    status = client.get("/api/playback/pool").json()
    assert status["active"] is True
    lanes = {lane["label"]: lane for lane in status["lanes"]}
    assert lanes["faster"]["remaining"] == 3 and lanes["slower"]["remaining"] == 1
    assert lanes["gone"]["zero_on_resolve"] and lanes["gone"]["exhausted"]
    assert status["ahead"] == 5
    assert status["starvation_config"]["check_interval_picks"] == 3
    assert dj_pool.get_pool().is_active()  # the singleton, not a throwaway


def test_post_pool_legacy_track_ids_still_works(client, specs):
    client.post("/api/playback/pool", json={"spec_id": None, "track_ids": [9, 10], "source_labels": {}})
    status = client.get("/api/playback/pool").json()
    assert [lane["label"] for lane in status["lanes"]] == ["default"]
    assert status["eligible_count"] == 2


def test_patch_on_active_spec_resyncs_lanes(client, specs):
    saved = client.post("/api/playback/mix-specs", json={
        "name": "ride", "request_text": "r", "sources": [{"kind": "folder", "query": "A", "label": "A"}],
    }).json()
    client.post("/api/playback/pool", json={"spec_id": saved["id"], "lanes": {"A": [1, 2]}})
    dj_pool.get_pool().state["lanes"]["A"]["played_count"] = 4
    r = client.patch("/api/playback/mix-specs/ride", json={
        "add_sources": [{"kind": "folder", "query": "B", "label": "B"}],
        "lanes": {"A": [1, 2], "B": [7, 8, 9]},
    }).json()
    assert r["pool_resynced"] is True
    assert [s["label"] for s in r["sources"]] == ["A", "B"]
    lanes = dj_pool.get_pool().state["lanes"]
    assert lanes["B"]["track_ids"] == [7, 8, 9]
    assert lanes["A"]["played_count"] == 4  # surviving lane keeps its history
    assert [s["label"] for s in client.get("/api/playback/mix-specs/ride").json()["sources"]] == ["A", "B"]


def test_patch_other_spec_leaves_pool_alone(client, specs):
    client.post("/api/playback/mix-specs", json={"name": "one", "sources": []})
    other = client.post("/api/playback/mix-specs", json={"name": "two", "sources": []}).json()
    client.post("/api/playback/pool", json={"spec_id": other["id"], "lanes": {"X": [1]}})
    r = client.patch("/api/playback/mix-specs/one", json={"lanes": {"Y": [5]}}).json()
    assert r["pool_resynced"] is False
    assert list(dj_pool.get_pool().state["lanes"]) == ["X"]


# ----- MCP tools go over HTTP -----


def test_mcp_source_has_no_pool_or_db_import():
    assert "audiplex.dj_pool" not in MCP_SOURCE
    assert "audiplex.database" not in MCP_SOURCE
    assert "sqlalchemy" not in MCP_SOURCE


@pytest.mark.parametrize("stopped", [True, False])
def test_dj_pool_stop_calls_delete(monkeypatch, stopped):
    calls = []

    async def fake_delete(path):
        calls.append(path)
        return {"stopped": stopped}

    monkeypatch.setattr(mcp_server, "_delete", fake_delete)
    out = asyncio.run(mcp_server.dj_pool_stop())
    assert calls == ["/api/playback/pool"]
    assert ("stopped the rolling pool" in out.lower()) is stopped


@pytest.mark.parametrize("stopped", [True, False])
def test_dj_mix_calls_delete_pool(monkeypatch, tmp_path, stopped):
    monkeypatch.setenv("DJ_MIX_SOURCES_FILE", str(tmp_path / "sources.json"))
    mcp_server._SWAP.update(task=None, status="none", detail="", ids=[])
    deletes = []

    async def fake_delete(path):
        deletes.append(path)
        return {"stopped": stopped}

    async def fake_get(path):
        if path.startswith("/api/playback/commands"):
            return []
        return {"playing": False, "track": None, "queue": []}

    async def fake_post(path, body):
        if path.endswith("/candidates/filter"):
            return {"allowed": body["track_ids"], "suppressed": []}
        return {"upcoming": body["new_ids"], "summary": "s"}

    async def fake_enqueue(cmd_type, payload):
        return {"id": 1}

    monkeypatch.setattr(mcp_server, "_delete", fake_delete)
    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_enqueue", fake_enqueue)
    out = asyncio.run(mcp_server.dj_mix(track_ids=[3, 4], shuffle=False))
    assert deletes == ["/api/playback/pool"]
    assert ("stopped the rolling pool" in out.lower()) is stopped


def test_dj_mix_refusal_does_not_stop_pool(monkeypatch):
    deletes = []

    async def fake_delete(path):
        deletes.append(path)
        return {"stopped": True}

    async def empty_source(kind, query, recursive=True):
        return f"folder '{query}'", []

    monkeypatch.setattr(mcp_server, "_delete", fake_delete)
    monkeypatch.setattr(mcp_server, "_resolve_source", empty_source)
    out = asyncio.run(mcp_server.dj_mix(sources=[{"kind": "folder", "query": "nope"}]))
    assert out.startswith("REFUSED")
    assert deletes == []


def test_dj_play_now_calls_delete_pool(monkeypatch):
    deletes = []

    async def fake_delete(path):
        deletes.append(path)
        return {"stopped": True}

    async def fake_enqueue(cmd_type, payload):
        return {"id": 9, "pending": 1}

    class _Resp:
        status_code = 200

        def json(self):
            return {"connected": True}

    class _Client:  # dj_play_now reads /device with a raw client
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            assert url.endswith("/api/playback/device")
            return _Resp()

    async def no_note(ids):
        return ""

    monkeypatch.setattr(mcp_server, "_delete", fake_delete)
    monkeypatch.setattr(mcp_server, "_enqueue", fake_enqueue)
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mcp_server, "_repeat_note", no_note)
    out = asyncio.run(mcp_server.dj_play_now([5]))
    assert deletes == ["/api/playback/pool"]
    assert "stopped the rolling pool" in out.lower()


def test_dj_spec_add_refuses_zero_track_source_without_patch(monkeypatch):
    patches = []

    async def fake_get(path):
        return {"id": 4, "name": "ride", "sources": [{"kind": "folder", "query": "A"}]}

    async def fake_patch(path, body):
        patches.append((path, body))
        return {}

    async def empty_source(kind, query, recursive=True):
        raise LookupError("no match")

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_patch", fake_patch)
    monkeypatch.setattr(mcp_server, "_resolve_source", empty_source)
    out = asyncio.run(mcp_server.dj_spec_add("ride", add_sources=[{"kind": "artist", "query": "Nobody"}]))
    assert out.startswith("REFUSED")
    assert patches == []


def test_dj_spec_add_patches_resolved_lanes_for_active_pool(monkeypatch):
    patches = []

    async def fake_get(path):
        if path == "/api/playback/pool":
            return {"active": True, "spec_id": 4}
        return {"id": 4, "name": "ride", "sources": [{"kind": "folder", "query": "A", "label": "A"}]}

    async def fake_patch(path, body):
        patches.append((path, body))
        return {"pool_resynced": True}

    async def resolve(kind, query, recursive=True):
        return query, [{"id": 1}, {"id": 2}] if query == "A" else [{"id": 9}]

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_patch", fake_patch)
    monkeypatch.setattr(mcp_server, "_resolve_source", resolve)
    out = asyncio.run(mcp_server.dj_spec_add("ride", add_sources=[{"kind": "artist", "query": "B", "label": "B"}]))
    path, body = patches[0]
    assert path == "/api/playback/mix-specs/ride"
    assert body["lanes"] == {"A": [1, 2], "B": [9]}
    assert "re-synced" in out


def test_dj_pool_set_posts_lanes_and_trims_once(monkeypatch):
    posts, sent = [], []

    async def fake_get(path):
        if path == "/api/playback/state":
            return {"playing": True, "track": {"id": 50}, "queue_index": 0,
                    "queue": [{"index": 0, "id": 50}, {"index": 1, "id": 51}]}
        if path.startswith("/api/playback/commands"):
            return []
        raise AssertionError(path)

    async def fake_post(path, body):
        posts.append((path, body))
        return {"per_lane_counts": {}, "initial_picks": [1, 3]}

    async def fake_enqueue(cmd_type, payload):
        sent.append((cmd_type, payload["track_ids"]))
        return {"id": 5}

    async def ok_ack(command_id, timeout=12.0):
        return {"ack_status": "ok"}

    async def resolve(kind, query, recursive=True):
        return query, [{"id": 1}, {"id": 2}] if query == "A" else [{"id": 3}]

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_enqueue", fake_enqueue)
    monkeypatch.setattr(mcp_server, "_await_ack", ok_ack)
    monkeypatch.setattr(mcp_server, "_resolve_source", resolve)
    out = asyncio.run(mcp_server.dj_pool_set(sources=[{"kind": "folder", "query": "A"},
                                                      {"kind": "folder", "query": "B"}], ahead=2))
    path, body = posts[0]
    assert path == "/api/playback/pool"
    assert body["lanes"] == {"A": [1, 2], "B": [3]}
    assert body["prime_current_id"] == 50 and body["ahead"] == 2
    assert sent == [("replace_upcoming", [1, 3])]
    assert "Pool set" in out


def test_dj_pool_set_refuses_empty_lane(monkeypatch):
    posts = []

    async def fake_post(path, body):
        posts.append(path)
        return {}

    async def resolve(kind, query, recursive=True):
        return query, []

    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_resolve_source", resolve)
    out = asyncio.run(mcp_server.dj_pool_set(sources=[{"kind": "folder", "query": "A"}]))
    assert out.startswith("REFUSED")
    assert posts == []
