import pytest

from audiplex import dj_pool


def _pool(tmp_path, lanes, turns=None):
    pool = dj_pool.DJPool(state_file=tmp_path / "pool.json")
    ids = [i for ids in lanes.values() for i in ids]
    pool.set_pool(
        1,
        ids,
        {},
        lanes=dj_pool.lanes_from_ids(lanes, None, turns),
        ahead=4,
        exclude_recent_hours=0,
    )
    return pool


def simulate(pool, n):
    picks = []
    for _ in range(n):
        name = pool._select_next_lane()
        if name is None:
            break
        lane = pool.state["lanes"][name]
        pool._advance_pass(lane)
        lane["last_pick_index"] = pool.state.get("round_robin_index", 0)
        pool.state["round_robin_index"] = pool.state.get("round_robin_index", 0) + 1
        picks.append(name)
    return picks


def _max_run(picks, name):
    best = cur = 0
    for p in picks:
        cur = cur + 1 if p == name else 0
        best = max(best, cur)
    return best


THREE = {"a": [1, 2, 3], "b": [4, 5, 6], "c": [7, 8, 9]}


def test_all_one_turn_is_strict_rotation(tmp_path):
    """#6730: with every lane at 1 turn the pick order is strict rotation."""
    pool = _pool(tmp_path, THREE)
    assert simulate(pool, 30) == ["a", "b", "c"] * 10


def test_all_one_matches_pre_turns_state(tmp_path):
    """#6730: lanes persisted without turns/pass keys still rotate strictly."""
    pool = _pool(tmp_path, THREE)
    for lane in pool.state["lanes"].values():
        lane.pop("turns", None)
        lane.pop("pass", None)
    assert simulate(pool, 30) == ["a", "b", "c"] * 10


def test_two_turns_gets_twice_the_picks(tmp_path):
    """#6730: a lane with 2 turns gets twice the picks and no long streaks."""
    pool = _pool(tmp_path, {"a": [1, 2, 3], "b": [4, 5, 6]}, {"a": 2})
    picks = simulate(pool, 30)
    assert picks.count("a") == 20
    assert picks.count("b") == 10
    assert _max_run(picks, "a") < 3
    for i in range(len(picks) - 2):
        assert "b" in picks[i : i + 3]


def test_three_lanes_mixed(tmp_path):
    """#6730: mixed turns 3/1/2 yield picks in proportion 30/10/20 over 60."""
    pool = _pool(tmp_path, THREE, {"a": 3, "b": 1, "c": 2})
    picks = simulate(pool, 60)
    assert (picks.count("a"), picks.count("b"), picks.count("c")) == (30, 10, 20)


def test_paused_lane_skipped_with_turns(tmp_path):
    """#6730: a paused lane is never picked even when other lanes have turns."""
    pool = _pool(tmp_path, THREE, {"a": 2})
    pool.state["lanes"]["b"]["paused"] = True
    picks = simulate(pool, 30)
    assert set(picks) == {"a", "c"}


def test_resumed_lane_does_not_burst(tmp_path):
    """#6730: a resumed lane does not get a catch-up burst of picks."""
    pool = _pool(tmp_path, THREE, {"a": 2})
    pool.state["lanes"]["c"]["paused"] = True
    simulate(pool, 30)
    pool.state["lanes"]["c"]["paused"] = False
    picks = simulate(pool, 12)
    assert _max_run(picks, "c") <= 2
    assert picks.count("c") <= 4


def test_set_lane_turns_live(tmp_path):
    """#6730: set_lane_turns changes weights live and ignores unknown labels."""
    pool = _pool(tmp_path, THREE)
    assert pool.set_lane_turns({"b": 4, "zz": 2}) == {"b": 4}
    picks = simulate(pool, 50)
    a, b, c = picks.count("a"), picks.count("b"), picks.count("c")
    assert b > a * 3 - 2
    assert abs(a - c) <= 1


@pytest.mark.parametrize(
    "value,expected",
    [(0, 1), (1, 1), (3, 3), ("3", 3), (9, 5), (None, 1), ("x", 1), (-2, 1)],
)
def test_clamp_turns(value, expected):
    """#6730: clamp_turns coerces to an int in 1..5, defaulting to 1."""
    assert dj_pool.clamp_turns(value) == expected


def test_source_label_and_turns_by_label():
    """#6730: source labels derive from label/query/kind; turns_by_label skips 1."""
    s1 = {"kind": "bucket", "query": "on the road", "label": "bucket: on the road", "turns": 2}
    s2 = {"kind": "folder", "query": "X:\\Music\\faster\\"}
    s3 = {"kind": "tracks", "turns": 7}
    assert dj_pool.source_label(s1) == "bucket: on the road"
    assert dj_pool.source_label(s2) == "faster"
    assert dj_pool.source_label(s3) == "tracks"
    assert dj_pool.turns_by_label([s1, s2, s3]) == {
        "bucket: on the road": 2,
        "tracks": 5,
    }


def test_resync_keeps_turns_and_override(tmp_path):
    """#6730: resync_lanes keeps surviving turns and honours overrides."""
    pool = _pool(tmp_path, {"a": [1, 2, 3], "b": [4, 5, 6]}, {"a": 2})
    pool.resync_lanes({"a": [1, 2], "d": [10]})
    assert pool.state["lanes"]["a"]["turns"] == 2
    assert pool.state["lanes"]["d"]["turns"] == 1
    pool.resync_lanes({"a": [1], "d": [10]}, turns={"d": 3})
    assert pool.state["lanes"]["d"]["turns"] == 3
    assert pool.state["lanes"]["a"]["turns"] == 2


# ---------------------------------------------------------------- routes


@pytest.fixture
def route_env(db_session, db_engine, client, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from audiplex.database import _migrate_create_dj_mix_specs
    from tests.test_1006_dj_priority import make_plain, patch_settings, patch_tilt

    _migrate_create_dj_mix_specs(db_engine)
    monkeypatch.setenv("AUDIPLEX_DJ_POOL_STATE", str(tmp_path / "p.json"))
    dj_pool.reset_pool_singleton()
    patch_settings(monkeypatch, [])
    patch_tilt(monkeypatch)
    ids = [make_plain(db_session, i) for i in range(1, 7)]
    yield SimpleNamespace(client=client, ids=ids)
    dj_pool.reset_pool_singleton()


def _start(env, sources, lanes):
    c = env.client
    r = c.post("/api/playback/mix-specs", json={"name": "pt", "request_text": "", "sources": sources})
    assert r.status_code in (200, 201)
    r = c.post("/api/playback/pool", json={"spec_id": r.json()["id"], "lanes": lanes})
    assert r.status_code == 200, r.text
    return dj_pool.get_pool().state["lanes"]


def test_route_pool_takes_turns_from_spec(route_env):
    """#6730: starting a set copies each source's turns onto its lane."""
    a, b = route_env.ids[:3], route_env.ids[3:]
    lanes = _start(route_env, [{"kind": "bucket", "query": "x", "label": "A", "turns": 2},
                               {"kind": "bucket", "query": "y", "label": "B"}], {"A": a, "B": b})
    assert lanes["A"]["turns"] == 2 and lanes["B"]["turns"] == 1


def test_route_patch_lane_turns_saves_and_goes_live(route_env):
    """#6730: PATCH lane_turns updates the saved source and the running lane."""
    a, b = route_env.ids[:3], route_env.ids[3:]
    _start(route_env, [{"kind": "bucket", "query": "x", "label": "A"},
                       {"kind": "bucket", "query": "y", "label": "B"}], {"A": a, "B": b})
    r = route_env.client.patch("/api/playback/mix-specs/pt", json={"lane_turns": {"B": 3}})
    assert r.status_code == 200 and r.json()["pool_resynced"] is True
    assert [s.get("turns") for s in r.json()["sources"]] == [None, 3]
    assert dj_pool.get_pool().state["lanes"]["B"]["turns"] == 3


def test_route_bucket_add_and_remove_reach_running_set(route_env, monkeypatch):
    """#6730: a web bucket add/remove (no lanes body) re-syncs the running set server-side."""
    a, b = route_env.ids[:3], route_env.ids[3:5]
    extra = route_env.ids[5:]
    _start(route_env, [{"kind": "bucket", "query": "x", "label": "A"}], {"A": a})
    monkeypatch.setattr("audiplex.routers.playback._bucket_lane_ids", lambda db, name: extra if name == "new" else b)
    c = route_env.client
    r = c.patch("/api/playback/mix-specs/pt", json={
        "add_sources": [{"kind": "bucket", "query": "new", "label": "bucket: new", "turns": 2}]})
    assert r.status_code == 200 and r.json()["pool_resynced"] is True
    lanes = dj_pool.get_pool().state["lanes"]
    assert lanes["bucket: new"]["track_ids"] == extra and lanes["bucket: new"]["turns"] == 2
    assert lanes["A"]["track_ids"] == a
    r = c.patch("/api/playback/mix-specs/pt", json={"remove_sources": ["bucket: new"]})
    assert r.status_code == 200
    assert set(dj_pool.get_pool().state["lanes"]) == {"A"}
