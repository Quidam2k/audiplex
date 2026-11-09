"""#5684: a song swiped out of the phone queue never comes back from the pool this set,
even through a replan or a reload (app relaunch / server restart); a new set clears it.
The refill only appends, so a manual reorder is never shuffled by it."""
from pathlib import Path

from audiplex.dj_pool import DJPool


def _pool(tmp_path) -> DJPool:
    pool = DJPool(state_file=str(tmp_path / "pool.json"))
    pool.set_pool(spec_id=1, track_ids=[10, 20, 30], source_labels={10: "A", 20: "A", 30: "A"}, ahead=2)
    return pool


def test_removed_never_repicked_even_after_replan_and_reload(tmp_path):
    pool = _pool(tmp_path)
    assert pool.remove_pick(20) is True and pool.remove_pick(20) is False
    reloaded = DJPool(state_file=str(tmp_path / "pool.json"))  # relaunch mid-set
    assert reloaded.state["removed_this_session"] == [20]
    for _ in range(3):
        picks = reloaded.top_up(current_track_id=1, upcoming_track_ids=[1])["picks"]
        assert 20 not in picks
    assert 20 not in reloaded.replan(current_track_id=1, upcoming_track_ids=[1, 10, 30], played_track_ids=[])


def test_no_active_set_records_nothing(tmp_path):
    pool = DJPool(state_file=str(tmp_path / "pool.json"))
    assert pool.remove_pick(20) is False and not pool.state.get("removed_this_session")


def test_new_set_clears_removed(tmp_path):
    pool = _pool(tmp_path)
    pool.remove_pick(20)
    pool.set_pool(spec_id=2, track_ids=[20], source_labels={20: "A"}, ahead=1)
    assert pool.state["removed_this_session"] == []
    assert pool.top_up(current_track_id=1, upcoming_track_ids=[1])["picks"] == [20]


def test_refill_leaves_queued_order_alone(tmp_path):  # Jarvis rider: no reorder over his manual order
    pool = _pool(tmp_path)
    picks = pool.top_up(current_track_id=1, upcoming_track_ids=[1, 30])["picks"]
    assert 30 not in picks and len(picks) == 1  # only new songs, for the END of the queue


def test_route(client, monkeypatch, tmp_path):
    from audiplex import dj_pool

    pool = _pool(tmp_path)
    monkeypatch.setattr(dj_pool, "get_pool", lambda: pool)
    r = client.post("/api/playback/pool/removed", json={"track_id": 30}).json()
    assert r["removed"] is True and r["removed_this_session"] == [30]
    assert client.post("/api/playback/pool/removed", json={}).status_code == 400
