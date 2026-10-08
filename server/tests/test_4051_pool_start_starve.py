"""#4051: a starting pool must not skip the queue tail it is about to replace.

10/8 replay: a one-lane 'whole albums' pool queued the 220 tracks that lane had
eligible. The ride-set re-pool then passed the WHOLE device queue as queued_ids, so
top_up skipped all 220 and 'whole albums' picked zero; a re-sync (which forgets the
replaced tail) fixed it. Since #4051 dj_pool_set passes only played + current.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from audiplex import dj_pool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402

CUR = 9000
ALBUMS = list(range(1, 221))        # every track the lane had eligible
FASTER = list(range(1001, 1101))


def _pool(tmp_path):
    lanes = {"whole albums": ALBUMS, "faster": FASTER}
    pool = dj_pool.DJPool(state_file=tmp_path / "pool.json")
    pool.set_pool(1, ALBUMS + FASTER, {}, lanes=dj_pool.lanes_from_ids(lanes),
                  ahead=60, exclude_recent_hours=0)
    return pool


@pytest.fixture
def identities(monkeypatch):
    """Real tracks carry recording/work ids (#7335): a queued track blocks its work,
    which is what stopped the lane recycling on 10/8. No DB: patch the lookups."""
    ids = {t: SimpleNamespace(recording_id=f"r{t}", work_id=f"w{t}") for t in [CUR, 8001, 8002, *ALBUMS, *FASTER]}
    monkeypatch.setattr(dj_pool, "build_identity_map", lambda db: ids)
    monkeypatch.setattr(dj_pool, "owner_user_id", lambda db: None)
    monkeypatch.setattr("audiplex.dj_bans.banned_ids", lambda db, identities=None: [])
    return object()  # stands in for the db session


def _albums_picked(result):
    return sum(1 for t in result["picks"] if t in set(ALBUMS))


def _state(history=(8001, 8002)):
    queue = [*history, CUR, *ALBUMS]  # played, current, then the one-lane pool's tail
    return {"track": {"id": CUR}, "queue_index": len(history),
            "queue": [{"id": t} for t in queue]}


def test_old_whole_queue_args_starve_the_lane(tmp_path, identities):
    """Reproduces 10/8: the whole queue as queued_ids leaves the lane nothing."""
    state = _state()
    r = _pool(tmp_path).top_up(CUR, [CUR], current_played_track_ids=[q["id"] for q in state["queue"]],
                               db=identities)
    assert _albums_picked(r) == 0
    why = next(d["why_empty"] for d in r["per_lane_details"] if d["label"] == "whole albums")
    assert "220 already queued or picked this session" in why  # #4051 not "a copy of ..."
    assert "0 a copy of a recording" in why


def test_pool_start_keeps_only_played_and_current(tmp_path, identities):
    state = _state()
    kept = mcp_server._pool_start_kept_ids(state)
    assert kept == [8001, 8002, CUR]
    r = _pool(tmp_path).top_up(CUR, [CUR], current_played_track_ids=kept, db=identities)
    assert _albums_picked(r) >= 20  # round-robin: about half of 60


def test_kept_ids_falls_back_to_current_position():
    state = _state()
    state["queue_index"] = 99  # stale / out of range
    assert mcp_server._pool_start_kept_ids(state) == [8001, 8002, CUR]
    assert mcp_server._pool_start_kept_ids({"track": {}, "queue": []}) == []


def test_undelivered_picks_are_forgotten(tmp_path):
    """#4051: 'Played this session: 220' after a refused send. Forgotten picks leave history."""
    pool = _pool(tmp_path)
    r = pool.top_up(CUR, [CUR])
    assert len(pool.state["played_this_session"]) == len(r["picks"]) > 0
    assert pool.forget_picks(r["picks"]) == len(r["picks"])
    assert pool.state["played_this_session"] == []
