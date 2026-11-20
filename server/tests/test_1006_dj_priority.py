"""#1006: the DJ favours Todd's YouTube folder + Cirque without looping them."""

import posixpath
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from audiplex import dj_pool, dj_priority
from audiplex.dj_priority import (
    PriorityGroup,
    boosted_weights,
    cooled,
    group_members,
    group_of,
    groups_from_config,
    not_capped,
    recent_sequence,
    share_blocked,
)
from audiplex.models import Album, Artist, DjTrackWeight, PlayStat, Track, User

YT_DIR = "/lib/Todd Walker - music"
YT_GROUP = {"name": "youtube", "folder_segments": ["Todd Walker - music"], "boost": 1.5}


# ---------------------------------------------------------------- helpers


def make_track(db, i, path, artist=None, kind=None):
    ar = Artist(name=artist or f"Artist {i}")
    db.add(ar)
    db.flush()
    al = Album(
        title=f"Album {i}",
        artist_id=ar.id,
        genre="Rock",
        folder_path=f"{posixpath.dirname(path)}#{i}",  # albums.folder_path is unique
    )
    db.add(al)
    db.flush()
    tr = Track(
        title=f"Title {i}",
        album_id=al.id,
        artist_id=ar.id,
        disc_number=1,
        track_number=1,
        duration_seconds=200,
        file_path=path,
        content_kind=kind,
    )
    db.add(tr)
    db.commit()
    return tr.id


def make_yt(db, i):
    return make_track(db, i, f"{YT_DIR}/yt{i}.opus")


def make_plain(db, i):
    return make_track(db, i, f"/lib/Other {i}/n{i}.mp3")


def add_start(db, track_id, ago):
    uid = db.query(User).first().id
    db.add(
        PlayStat(
            track_id=track_id,
            user_id=uid,
            event="start",
            played_seconds=0,
            timestamp=datetime.utcnow() - ago,
        )
    )
    db.commit()


def patch_settings(monkeypatch, groups, **extra):
    import audiplex.config as cfg

    real = cfg.get_settings()
    values = {
        "dj_priority_groups": groups,
        "dj_priority_cooldown_days": 7,
        "dj_priority_max_share": 1.0,
        "dj_priority_share_window": 3,
    }
    values.update(extra)
    s = real.model_copy(update=values)
    monkeypatch.setattr("audiplex.config.get_settings", lambda: s)
    return s


def patch_tilt(monkeypatch):
    monkeypatch.setattr(
        dj_pool,
        "_context_tilt",
        lambda *a, **k: SimpleNamespace(
            weights=lambda seed, c, w: (w, {}), enabled=False, gave_up=0
        ),
    )


def pick_ids(result):
    out = []
    for p in result["picks"]:
        if isinstance(p, dict):
            out.append(p.get("track_id", p.get("id")))
        else:
            out.append(p)
    return out


def run_pool(tmp_path, db, name, lanes, cur, ahead):
    pool = dj_pool.DJPool(state_file=tmp_path / name)
    flat = [t for ids in lanes.values() for t in ids]
    pool.set_pool(
        1,
        flat,
        {},
        lanes=dj_pool.lanes_from_ids(lanes),
        ahead=ahead,
        exclude_recent_hours=0,
    )
    uid = db.query(User).first().id
    result = pool.top_up(
        current_track_id=cur,
        upcoming_track_ids=[cur],
        current_played_track_ids=[cur],
        db=db,
        owner_id=uid,
    )
    return pool, result


class _Rec(dict):
    """recording_of mapping that also works when called like a function."""

    def __call__(self, tid):
        return self.get(tid)


# ---------------------------------------------------------------- 1


def test_groups_from_config_skips_and_clamps():
    groups = groups_from_config(
        [
            {},
            {"name": "x"},
            {"name": "hi", "folder_segments": ["a"], "boost": 9},
            {"name": "lo", "artists": ["b"], "boost": 0.2},
            {"name": "def", "artists": ["c"]},
        ]
    )
    by_name = {g.name: g for g in groups}
    assert set(by_name) == {"hi", "lo", "def"}
    assert by_name["hi"].boost == 4.0
    assert by_name["lo"].boost == 1.0
    assert by_name["def"].boost == 1.5


# ---------------------------------------------------------------- 2


def test_group_members_folder_artist_and_exclusions(db_session):
    db = db_session
    yt = make_track(db, 1, "/lib/Todd Walker - music/x.opus")
    cq_folder = make_track(db, 2, "/lib/Cirque Du Soleil/Alegria/y.mp3")
    cq_artist = make_track(db, 3, "/lib/Mixed/z.mp3", artist="CIRQUE DU SOLEIL")
    filename_only = make_track(
        db, 4, "/lib/Mixed/Cirque Du Soleil.mp3", artist="Somebody Else"
    )
    podcast = make_track(
        db, 5, "/lib/Todd Walker - music/pod.opus", kind="podcast"
    )
    music = make_track(
        db, 6, "/lib/Todd Walker - music/m.opus", kind="music"
    )

    groups = [
        PriorityGroup("youtube", folder_segments=("todd walker - music",)),
        PriorityGroup(
            "cirque",
            folder_segments=("cirque du soleil",),
            artists=("Cirque Du Soleil",),
        ),
    ]
    members = group_members(db, groups)
    assert members["youtube"] == {yt, music}
    assert members["cirque"] == {cq_folder, cq_artist}
    assert filename_only not in members["cirque"]
    assert filename_only not in members["youtube"]
    assert podcast not in members["youtube"]


# ---------------------------------------------------------------- 3


def test_boosted_weights_largest_boost_wins():
    a, b, c = 1, 2, 3
    groups = [
        PriorityGroup("g1", artists=("x",), boost=1.5),
        PriorityGroup("g2", artists=("y",), boost=2.0),
    ]
    members = {"g1": {a, b, c}, "g2": {c}}
    weights = {a: 0.5, b: 0.0}
    out = boosted_weights(weights, members, groups)
    assert out is not weights
    assert out[a] == pytest.approx(0.75)
    assert out[b] == 0.0
    assert out[c] == pytest.approx(2.0)
    assert weights == {a: 0.5, b: 0.0}

    only = boosted_weights(weights, members, groups, only={a})
    assert only[a] == pytest.approx(0.75)
    assert only.get(c, 1.0) == 1.0
    assert weights == {a: 0.5, b: 0.0}


# ---------------------------------------------------------------- 4


def test_cooled_by_id_and_recording():
    now = datetime.now(timezone.utc)
    members = {"g": {1, 2, 3}}
    recording_of = _Rec({1: "rec1", 2: "rec2", 3: "rec3"})

    def play(tid, days, rec=None):
        return SimpleNamespace(
            track_id=tid, recording_id=rec, at=now - timedelta(days=days)
        )

    assert 1 in cooled([play(1, 6)], members, recording_of, 7, now)
    assert 1 not in cooled([play(1, 8)], members, recording_of, 7, now)
    # non-member never cooled
    assert 99 not in cooled([play(99, 1)], members, recording_of, 7, now)
    # member matched through the played recording
    res = cooled([play(50, 1, rec="rec3")], members, recording_of, 7, now)
    assert 3 in res
    assert 2 not in res


# ---------------------------------------------------------------- 5


def test_share_blocked_and_not_capped():
    m, x = 1, 2
    gbt = group_of({"g": {m}})
    assert share_blocked(gbt, [m, x], 3, 0.34) == {"g"}
    assert share_blocked(gbt, [x, x], 3, 0.34) == set()
    assert share_blocked(gbt, [m, x], 0, 0.34) == set()
    assert share_blocked(gbt, [m, x], 3, 1.0) == set()
    assert not_capped([m], gbt, {"g"}) == []
    assert not_capped([m, x], gbt, {"g"}) == [x]


# ---------------------------------------------------------------- 6


def test_recent_sequence_order_and_exclude():
    plays = [
        SimpleNamespace(track_id=1, event="start", minutes_ago=5),
        SimpleNamespace(track_id=2, event="start", minutes_ago=50),
        SimpleNamespace(track_id=3, event="start", minutes_ago=20),
        SimpleNamespace(track_id=4, event="skip", minutes_ago=10),
        SimpleNamespace(track_id=5, event="start", minutes_ago=500),
    ]
    assert recent_sequence(plays, 60, set()) == [2, 3, 1]
    assert recent_sequence(plays, 60, {3}) == [2, 1]


# ---------------------------------------------------------------- 7


@pytest.mark.parametrize("days,expect_present", [(2, False), (8, True)])
def test_pool_cooldown(db_session, tmp_path, monkeypatch, days, expect_present):
    db = db_session
    patch_settings(monkeypatch, [YT_GROUP])
    patch_tilt(monkeypatch)
    cur = make_plain(db, 100)
    m1 = make_yt(db, 1)
    n1 = make_plain(db, 2)
    n2 = make_plain(db, 3)
    add_start(db, m1, timedelta(days=days))

    seen = False
    for k in range(20):
        _, res = run_pool(tmp_path, db, f"pool{k}.json", {"A": [m1, n1, n2]}, cur, 2)
        ids = pick_ids(res)
        if m1 in ids:
            seen = True
            assert expect_present
    assert seen == expect_present


# ---------------------------------------------------------------- 8


def test_pool_share_cap(db_session, tmp_path, monkeypatch):
    db = db_session
    patch_settings(
        monkeypatch, [YT_GROUP], dj_priority_max_share=0.34, dj_priority_share_window=3
    )
    patch_tilt(monkeypatch)
    cur = make_plain(db, 100)
    p = [make_yt(db, i) for i in range(1, 7)]
    q = [make_plain(db, i) for i in range(10, 16)]
    members = set(p)

    for k in range(5):
        _, res = run_pool(tmp_path, db, f"cap{k}.json", {"P": p, "Q": q}, cur, 6)
        ids = pick_ids(res)
        assert len(ids) == 6
        seq = [cur] + ids
        for i in range(len(seq) - 2):
            window = seq[i : i + 3]
            assert sum(1 for t in window if t in members) < 2


# ---------------------------------------------------------------- 9


def test_pool_cap_fallback_still_picks(db_session, tmp_path, monkeypatch):
    db = db_session
    patch_settings(
        monkeypatch, [YT_GROUP], dj_priority_max_share=0.34, dj_priority_share_window=3
    )
    patch_tilt(monkeypatch)
    cur = make_plain(db, 100)
    p = [make_yt(db, i) for i in range(1, 7)]
    _, res = run_pool(tmp_path, db, "fb.json", {"P": p}, cur, 4)
    assert len(pick_ids(res)) == 4


# ---------------------------------------------------------------- 10


def test_pool_boost_reaches_pick(db_session, tmp_path, monkeypatch):
    db = db_session
    patch_settings(monkeypatch, [YT_GROUP])
    patch_tilt(monkeypatch)
    cur = make_plain(db, 100)
    m1 = make_yt(db, 1)
    m2 = make_yt(db, 2)
    db.add(DjTrackWeight(track_id=m1, weight=0.5, skip_rides=0, reason="test"))
    db.commit()

    recorded = []

    def fake_pick(*args, **kwargs):
        values = list(args) + list(kwargs.values())
        weights = next((v for v in values if isinstance(v, dict)), None)
        cands = next((v for v in values if isinstance(v, (list, tuple))), None)
        recorded.append(dict(weights) if weights is not None else {})
        return cands[0]

    monkeypatch.setattr(dj_pool, "_weighted_pick", fake_pick)
    run_pool(tmp_path, db, "boost.json", {"A": [m1, m2]}, cur, 1)

    assert recorded
    first = recorded[0]
    assert first[m1] == pytest.approx(0.75)
    assert first[m2] == pytest.approx(1.5)


# ---------------------------------------------------------------- 11


def test_no_groups_no_priority_state(db_session, tmp_path, monkeypatch):
    db = db_session
    patch_settings(monkeypatch, [])
    patch_tilt(monkeypatch)
    cur = make_plain(db, 100)
    ids = [make_plain(db, i) for i in range(1, 5)]
    pool, res = run_pool(tmp_path, db, "none.json", {"A": ids}, cur, 3)
    assert "priority" not in pool.state
    assert len(pick_ids(res)) == 3


def test_priority_state_when_configured(db_session, tmp_path, monkeypatch):
    db = db_session
    patch_settings(monkeypatch, [YT_GROUP])
    patch_tilt(monkeypatch)
    cur = make_plain(db, 100)
    m1 = make_yt(db, 1)
    n1 = make_plain(db, 2)
    n2 = make_plain(db, 3)
    pool, _ = run_pool(tmp_path, db, "state.json", {"A": [m1, n1, n2]}, cur, 2)
    pr = pool.state["priority"]
    assert "youtube" in pr["groups"]
    assert "cooled" in pr


# ---------------------------------------------------------------- 12


def _spec_list(data):
    if isinstance(data, dict):
        for key in ("specs", "items", "mix_specs"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data


def _lanes(client, resp):
    """The singleton pool's lanes ({label: [ids]}) after a POST /pool."""
    assert resp.status_code == 200
    return {k: v["track_ids"] for k, v in dj_pool.get_pool().state["lanes"].items()}


@pytest.fixture
def route_env(db_session, db_engine, client, tmp_path, monkeypatch):
    from audiplex.database import _migrate_create_dj_mix_specs

    _migrate_create_dj_mix_specs(db_engine)
    monkeypatch.setenv("AUDIPLEX_DJ_POOL_STATE", str(tmp_path / "p.json"))
    dj_pool.reset_pool_singleton()
    patch_settings(monkeypatch, [YT_GROUP])
    patch_tilt(monkeypatch)
    m = make_yt(db_session, 1)
    n1 = make_plain(db_session, 2)
    yield SimpleNamespace(client=client, member=m, n1=n1)
    dj_pool.reset_pool_singleton()


def test_routes_priority_lane_flag_sticks(route_env):
    c = route_env.client
    r = c.post(
        "/api/playback/mix-specs",
        json={
            "name": "home",
            "request_text": "",
            "sources": [],
            "priority_lane": True,
        },
    )
    assert r.status_code in (200, 201)
    r = c.post(
        "/api/playback/mix-specs",
        json={"name": "home", "request_text": "", "sources": []},
    )
    assert r.status_code in (200, 201)
    listing = _spec_list(c.get("/api/playback/mix-specs").json())
    home = [s for s in listing if s.get("name") == "home"]
    assert home
    assert home[0]["priority_lane"] is True


def test_routes_pool_priorities_lane_follows_spec(route_env):
    c = route_env.client
    r = c.post(
        "/api/playback/mix-specs",
        json={
            "name": "home",
            "request_text": "",
            "sources": [],
            "priority_lane": True,
        },
    )
    spec_id = r.json()["id"]
    r = c.post(
        "/api/playback/pool",
        json={"spec_id": spec_id, "lanes": {"A": [route_env.n1]}},
    )
    assert r.status_code == 200
    lanes = _lanes(c, r)
    assert "priorities" in lanes
    assert route_env.member in lanes["priorities"]


def test_routes_pool_spec_without_flag_has_no_priorities(route_env):
    c = route_env.client
    r = c.post(
        "/api/playback/mix-specs",
        json={"name": "plain", "request_text": "", "sources": []},
    )
    spec_id = r.json()["id"]
    r = c.post(
        "/api/playback/pool",
        json={"spec_id": spec_id, "lanes": {"A": [route_env.n1]}},
    )
    assert r.status_code == 200
    assert "priorities" not in _lanes(c, r)


def test_routes_pool_flag_without_spec(route_env):
    c = route_env.client
    r = c.post(
        "/api/playback/pool",
        json={"lanes": {"A": [route_env.n1]}, "priority_lane": True},
    )
    assert r.status_code == 200
    lanes = _lanes(c, r)
    assert "priorities" in lanes
    assert route_env.member in lanes["priorities"]


def test_routes_pool_no_spec_no_flag(route_env):
    c = route_env.client
    r = c.post("/api/playback/pool", json={"lanes": {"A": [route_env.n1]}})
    assert r.status_code == 200
    assert "priorities" not in _lanes(c, r)


# ---------------------------------------------------------------- Codex review regressions


def test_all_cooled_pool_still_plays(db_session, tmp_path, monkeypatch):
    """The cooldown is a preference: a lane of only cooled members still fills."""
    db = db_session
    patch_settings(monkeypatch, [YT_GROUP])
    patch_tilt(monkeypatch)
    cur = make_plain(db, 100)
    m = [make_yt(db, i) for i in range(1, 4)]
    for t in m:
        add_start(db, t, timedelta(days=2))
    _, res = run_pool(tmp_path, db, "cooled.json", {"A": m}, cur, 2)
    assert len(pick_ids(res)) == 2


def test_one_track_window_still_caps():
    assert share_blocked({1: {"g"}}, [], 1, 0.34) == {"g"}
    assert share_blocked({}, [5], 1, 0.34) == set()


def test_malformed_group_config_is_ignored():
    assert groups_from_config([{"name": "bad", "folder_segments": 1}]) == []
    (g,) = groups_from_config([{"name": "one", "folder_segments": "Todd Walker - music"}])
    assert g.folder_segments == ("Todd Walker - music",)


def test_spec_edit_keeps_priorities_lane(tmp_path):
    pool = dj_pool.DJPool(state_file=tmp_path / "resync.json")
    lanes = {"A": [1, 2], dj_pool.PRIORITY_LANE: [7, 8]}
    pool.set_pool(1, [1, 2, 7, 8], {}, lanes=dj_pool.lanes_from_ids(lanes))
    pool.state["lanes"][dj_pool.PRIORITY_LANE]["injected"] = True  # as POST /pool marks it
    pool.resync_lanes({"A": [1, 2], "B": [3]})
    assert pool.state["lanes"][dj_pool.PRIORITY_LANE]["track_ids"] == [7, 8]
    assert set(pool.state["lanes"]) == {"A", "B", dj_pool.PRIORITY_LANE}


def test_opting_in_keeps_a_long_track_named_alone(route_env, db_session):
    """A long member asked for in a one-track lane stays there when the lane is on."""
    make_yt(db_session, 50)  # a 2+ track priorities lane gets no one-track exemption
    t = db_session.get(Track, route_env.member)
    t.duration_seconds = 25 * 60
    db_session.commit()
    r = route_env.client.post("/api/playback/pool", json={
        "lanes": {"named": [route_env.member]}, "priority_lane": True})
    lanes = _lanes(route_env.client, r)
    assert lanes["named"] == [route_env.member]
    assert route_env.member not in lanes.get(dj_pool.PRIORITY_LANE, [])


def test_bad_config_still_starts_the_pool(route_env, monkeypatch):
    patch_settings(monkeypatch, [{"name": "bad", "folder_segments": 1}])
    r = route_env.client.post("/api/playback/pool", json={
        "lanes": {"A": [route_env.n1]}, "priority_lane": True})
    assert dj_pool.PRIORITY_LANE not in _lanes(route_env.client, r)


def test_spec_edit_drops_a_user_lane_named_priorities(tmp_path):
    pool = dj_pool.DJPool(state_file=tmp_path / "resync2.json")
    lanes = {"A": [1], dj_pool.PRIORITY_LANE: [7]}
    pool.set_pool(1, [1, 7], {}, lanes=dj_pool.lanes_from_ids(lanes))
    pool.resync_lanes({"A": [1]})
    assert dj_pool.PRIORITY_LANE not in pool.state["lanes"]


def test_post_pool_marks_the_injected_lane(route_env):
    r = route_env.client.post("/api/playback/pool", json={
        "lanes": {"A": [route_env.n1]}, "priority_lane": True})
    assert r.status_code == 200
    assert dj_pool.get_pool().state["lanes"][dj_pool.PRIORITY_LANE]["injected"] is True
