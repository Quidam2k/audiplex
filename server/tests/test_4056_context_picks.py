"""#4056: pool picks lean on song memory - a boost only, fail-open, rotation untouched."""

import time
import random

from audiplex import dj_context_picks, dj_pool
from audiplex.dj_pool import DJPool


LANE_A = list(range(101, 111))
LANE_B = list(range(201, 211))


def _link(track_id):
    return {
        "track_id": track_id,
        "score": 5,
        "reasons": [{"kind": "dj_note", "text": "great segue", "source": "RFL"}],
    }


def _one_per_lane(picks):
    return len(picks) == 2 and {p in LANE_A for p in picks} == {True, False}


def _make_pool(tmp_path, filename="pool.json"):
    pool = DJPool(state_file=str(tmp_path / filename))
    pool.set_pool(
        spec_id=1,
        track_ids=LANE_A + LANE_B,
        source_labels={
            **{tid: "A" for tid in LANE_A},
            **{tid: "B" for tid in LANE_B},
        },
        lanes=dj_pool.lanes_from_ids({"A": LANE_A, "B": LANE_B}),
        ahead=2,
    )
    return pool


def test_context_weights_boost_linked_candidates():
    tilt = dj_context_picks.ContextTilt(
        True, after_fn=lambda seed, n: [_link(2)]
    )
    base = {3: 0.5}

    tilted, hits = tilt.weights(9, [1, 2, 3], base)

    assert tilted == {3: 0.5, 2: 2.0}
    assert base == {3: 0.5}
    assert set(hits) == {2}
    assert dj_context_picks.reason_text(9, hits[2]) == (
        "plays well after #9: great segue"
    )


def test_context_boost_stacks_on_learned_weight():
    tilt = dj_context_picks.ContextTilt(
        True, after_fn=lambda seed, n: [_link(2)]
    )

    tilted, _ = tilt.weights(9, [2], {2: 0.5})

    assert tilted[2] == 1.0


def test_disabled_tilt_and_invalid_seeds_never_call_after():
    calls = []

    def after(seed, n):
        calls.append(seed)
        raise AssertionError("after_fn must not be called")

    base = {2: 0.5}
    for enabled, seed in [(False, 9), (True, None), (True, 0), (True, -1)]:
        tilt = dj_context_picks.ContextTilt(enabled, after_fn=after)
        assert tilt.weights(seed, [2], base) == (base, {})
        assert tilt.gave_up is None

    assert calls == []


def test_context_exception_fails_open_and_stops_retrying():
    calls = []

    def after(seed, n):
        calls.append(seed)
        raise RuntimeError("song memory unavailable")

    tilt = dj_context_picks.ContextTilt(True, after_fn=after)
    base = {2: 0.5}

    assert tilt.weights(9, [2], base) == (base, {})
    assert tilt.gave_up
    assert tilt.weights(10, [2], base) == (base, {})
    assert calls == [9]


def test_context_time_budget_fails_open_quickly():
    tilt = dj_context_picks.ContextTilt(
        True,
        budget_s=0.05,
        after_fn=lambda seed, n: time.sleep(0.5) or [],
    )
    base = {2: 0.5}

    started = time.monotonic()
    result = tilt.weights(9, [2], base)
    elapsed = time.monotonic() - started

    assert result == (base, {})
    assert elapsed < 0.4
    assert tilt.gave_up


def test_context_caches_links_for_each_seed():
    calls = []

    def after(seed, n):
        calls.append(seed)
        return [_link(2)]

    tilt = dj_context_picks.ContextTilt(True, after_fn=after)

    assert tilt.weights(9, [2], {})[0] == {2: 2.0}
    assert tilt.weights(9, [2, 3], {2: 0.5})[0] == {2: 1.0}
    assert calls == [9]


def test_pool_tilt_boosts_picks_without_changing_rotation(tmp_path, monkeypatch):
    random.seed(1234)

    def after(seed, n):  # 105 plays well after anything
        return [_link(105)]

    monkeypatch.setattr(
        dj_pool,
        "_context_tilt",
        lambda: dj_context_picks.ContextTilt(True, after_fn=after),
    )
    count = 0
    for i in range(200):
        pool = _make_pool(tmp_path, f"p{i}.json")
        picks = pool.top_up(
            current_track_id=999, upcoming_track_ids=[999], db=None
        )["picks"]
        assert _one_per_lane(picks)  # rotation untouched
        count += 105 in picks

    assert count > 28  # untilted mean is 20 (1 in 10)


def test_pool_reports_context_reasons(tmp_path, monkeypatch):
    def after(seed, n):
        return [_link(tid) for tid in LANE_A]

    monkeypatch.setattr(
        dj_pool,
        "_context_tilt",
        lambda: dj_context_picks.ContextTilt(True, after_fn=after),
    )
    pool = _make_pool(tmp_path)

    out = pool.top_up(current_track_id=999, upcoming_track_ids=[999], db=None)

    pick = next(p for p in out["picks"] if p in LANE_A)
    assert len(out["context_reasons"]) == 1
    assert out["context_reasons"][0].startswith(f"#{pick} plays well after #")
    assert pool.status()["context_picks"]["enabled"] is True


def test_pool_context_failure_keeps_picking(tmp_path, monkeypatch):
    def after(seed, n):
        raise RuntimeError("song memory unavailable")

    monkeypatch.setattr(
        dj_pool,
        "_context_tilt",
        lambda: dj_context_picks.ContextTilt(True, after_fn=after),
    )
    pool = _make_pool(tmp_path)

    out = pool.top_up(current_track_id=999, upcoming_track_ids=[999], db=None)

    assert _one_per_lane(out["picks"])
    assert pool.status()["context_picks"]["skipped"]


def test_pool_default_context_tilt_is_disabled(tmp_path):
    pool = _make_pool(tmp_path)

    out = pool.top_up(current_track_id=999, upcoming_track_ids=[999], db=None)

    assert out["context_reasons"] == []
    assert pool.status()["context_picks"]["enabled"] is False
