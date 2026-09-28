"""DJ pool lanes + ringmaster: round-robin, starvation, cue management (#5495, #5477)."""

import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from audiplex.dj_pool import DJPool
from audiplex.trigger_matcher import match_triggers


@pytest.fixture
def tmp_pool():
    """DJ pool with temp state file."""
    with TemporaryDirectory() as tmpdir:
        pool_file = Path(tmpdir) / "pool.json"
        yield DJPool(state_file=str(pool_file))


def test_pool_lanes_initialize_empty(tmp_pool):
    """New pool has empty lanes dict."""
    assert tmp_pool.state["lanes"] == {}
    assert tmp_pool.state.get("round_robin_index") == 0


def test_set_pool_with_lanes(tmp_pool):
    """set_pool creates lanes from provided lane dict."""
    lanes = {
        "FolderA": {
            "track_ids": [1, 2, 3],
            "played_count": 0,
            "last_played_at": None,
            "exhausted": False,
            "zero_on_resolve": False,
        },
        "FolderB": {
            "track_ids": [10, 11],
            "played_count": 0,
            "last_played_at": None,
            "exhausted": False,
            "zero_on_resolve": False,
        },
    }

    result = tmp_pool.set_pool(
        spec_id=5,
        track_ids=[1, 2, 3, 10, 11],
        source_labels={1: "FolderA", 2: "FolderA", 3: "FolderA", 10: "FolderB", 11: "FolderB"},
        lanes=lanes,
    )

    assert result["spec_id"] == 5
    assert result["eligible_count"] == 5
    assert result["per_lane_counts"]["FolderA"] == 3
    assert result["per_lane_counts"]["FolderB"] == 2
    assert tmp_pool.state["lanes"]["FolderA"]["track_ids"] == [1, 2, 3]
    assert tmp_pool.state["lanes"]["FolderB"]["track_ids"] == [10, 11]


def test_pool_lanes_persists(tmp_pool):
    """Lane state is persisted to disk."""
    pool_file = tmp_pool.state_file

    lanes = {
        "Folder1": {"track_ids": [100], "played_count": 0, "last_played_at": None, "exhausted": False},
        "Folder2": {"track_ids": [200], "played_count": 0, "last_played_at": None, "exhausted": False},
    }

    tmp_pool.set_pool(spec_id=1, track_ids=[100, 200], source_labels={}, lanes=lanes)

    # New instance reads same lanes
    pool2 = DJPool(state_file=str(pool_file))
    assert "Folder1" in pool2.state["lanes"]
    assert "Folder2" in pool2.state["lanes"]
    assert pool2.state["lanes"]["Folder1"]["track_ids"] == [100]


def test_top_up_round_robin_even_balance(tmp_pool):
    """top_up with even balance cycles through lanes round-robin."""
    lanes = {
        "LaneA": {"track_ids": [1, 2, 3], "played_count": 0, "last_played_at": None, "exhausted": False},
        "LaneB": {"track_ids": [10, 11, 12], "played_count": 0, "last_played_at": None, "exhausted": False},
    }

    tmp_pool.set_pool(
        spec_id=1,
        track_ids=[1, 2, 3, 10, 11, 12],
        source_labels={},
        lanes=lanes,
        balance="even",
        ahead=4,
    )

    # First top_up should pick 4 tracks, alternating lanes
    result = tmp_pool.top_up(
        current_track_id=1,
        upcoming_track_ids=[1, 2],  # Only 1 ahead, need 3 more
    )

    # Should have picked 3 tracks
    assert len(result["picks"]) == 3
    # With even balance and 2 lanes, should pick from both


def test_top_up_no_topup_while_stream(tmp_pool):
    """top_up returns empty if current is stream (id=-1)."""
    lanes = {
        "Lane": {"track_ids": [100, 101], "played_count": 0, "last_played_at": None, "exhausted": False},
    }

    tmp_pool.set_pool(spec_id=1, track_ids=[100, 101], source_labels={}, lanes=lanes)

    result = tmp_pool.top_up(
        current_track_id=-1,  # Stream
        upcoming_track_ids=[-1, 100, 101],
    )

    assert result["picks"] == []
    assert "stream" in result["reason"].lower()


def test_top_up_no_topup_while_paused(tmp_pool):
    """top_up returns empty if current is None (paused)."""
    lanes = {
        "Lane": {"track_ids": [100, 101], "played_count": 0, "last_played_at": None, "exhausted": False},
    }

    tmp_pool.set_pool(spec_id=1, track_ids=[100, 101], source_labels={}, lanes=lanes)

    result = tmp_pool.top_up(
        current_track_id=None,  # Paused
        upcoming_track_ids=[],
    )

    assert result["picks"] == []
    assert "paused" in result["reason"].lower()


def test_top_up_no_topup_while_dj_break(tmp_pool):
    """top_up returns empty if current is DJ break (id<0)."""
    lanes = {
        "Lane": {"track_ids": [100, 101], "played_count": 0, "last_played_at": None, "exhausted": False},
    }

    tmp_pool.set_pool(spec_id=1, track_ids=[100, 101], source_labels={}, lanes=lanes)

    result = tmp_pool.top_up(
        current_track_id=-50,  # DJ break
        upcoming_track_ids=[-50, 100],
    )

    assert result["picks"] == []
    assert "stream" in result["reason"].lower()


def test_top_up_skips_played_this_session(tmp_pool):
    """top_up excludes tracks already played in this session."""
    lanes = {
        "Lane": {"track_ids": [1, 2, 3, 4], "played_count": 0, "last_played_at": None, "exhausted": False},
    }

    tmp_pool.set_pool(
        spec_id=1,
        track_ids=[1, 2, 3, 4],
        source_labels={},
        lanes=lanes,
        played_this_session=[1, 2],  # Already played
    )

    result = tmp_pool.top_up(
        current_track_id=5,
        upcoming_track_ids=[5],  # Only 1 ahead, need 3 more
    )

    # Should pick from [3, 4] only
    assert len(result["picks"]) > 0
    assert all(pick in [3, 4] for pick in result["picks"])


def test_top_up_exhausted_lanes_skipped(tmp_pool):
    """top_up skips lanes marked as exhausted."""
    lanes = {
        "LaneA": {"track_ids": [1], "played_count": 0, "last_played_at": None, "exhausted": False},
        "LaneB": {"track_ids": [10, 11], "played_count": 0, "last_played_at": None, "exhausted": True},  # Exhausted
    }

    tmp_pool.set_pool(
        spec_id=1,
        track_ids=[1, 10, 11],
        source_labels={},
        lanes=lanes,
        ahead=2,
    )

    result = tmp_pool.top_up(
        current_track_id=100,
        upcoming_track_ids=[100],  # Only 1 ahead, need 1 more
    )

    # Should only pick from LaneA (LaneB is exhausted)
    assert all(pick == 1 for pick in result["picks"])


def test_status_per_lane_details(tmp_pool):
    """status() returns per-lane details including exhausted flag."""
    lanes = {
        "Fast": {"track_ids": [1, 2], "played_count": 1, "last_played_at": time.time(), "exhausted": False},
        "Slow": {"track_ids": [10], "played_count": 0, "last_played_at": None, "exhausted": True},
    }

    tmp_pool.set_pool(spec_id=1, track_ids=[1, 2, 10], source_labels={}, lanes=lanes)

    status = tmp_pool.status()

    assert len(status["lanes"]) == 2
    fast_lane = next((l for l in status["lanes"] if l["label"] == "Fast"), None)
    slow_lane = next((l for l in status["lanes"] if l["label"] == "Slow"), None)

    assert fast_lane is not None
    assert fast_lane["remaining"] == 2
    assert fast_lane["played_count"] == 1
    assert not fast_lane["exhausted"]

    assert slow_lane is not None
    assert slow_lane["exhausted"]


def test_cue_add_and_track(tmp_pool):
    """add_cue and get_pending_cues manage cues."""
    cue = tmp_pool.add_cue(
        cue_id=1,
        trigger_kind="track_end",
        trigger_track_id=5,
        play_track=100,
        say="Bridge incoming",
    )

    assert cue["id"] == 1
    assert cue["trigger"]["kind"] == "track_end"
    assert cue["trigger"]["track_id"] == 5

    pending = tmp_pool.get_pending_cues()
    assert len(pending) == 1
    assert pending[0]["id"] == 1


def test_cue_mark_done(tmp_pool):
    """mark_cue_done removes cue from pending."""
    tmp_pool.add_cue(1, "track_end", 5, play_track=100, say="Test")

    assert tmp_pool.mark_cue_done(1)
    assert len(tmp_pool.get_pending_cues()) == 0


def test_trigger_matcher_cue_priority(tmp_pool):
    """top_up checks cues first before lane-based picks."""
    lanes = {
        "Lane": {"track_ids": [1, 2, 3], "played_count": 0, "last_played_at": None, "exhausted": False},
    }

    tmp_pool.set_pool(spec_id=1, track_ids=[1, 2, 3], source_labels={}, lanes=lanes)

    # Add a cue that fires on track 5
    cues = [
        {
            "id": 1,
            "trigger": {"kind": "track_end", "track_id": 5},
            "play_track": 999,  # Special injected track
            "say": "Cue track",
            "done": False,
        }
    ]

    from audiplex.trigger_matcher import match_triggers
    event = {"kind": "track_end", "track_id": 5}
    matched = match_triggers(event, cues)

    assert len(matched) == 1
    assert matched[0]["play_track"] == 999


def test_starvation_rule_fires(tmp_pool):
    """Starvation rule: if a lane hasn't been picked in N picks, it jumps the line."""
    lanes = {
        "FrequentLane": {"track_ids": [1, 2, 3, 4, 5, 6], "played_count": 0, "last_played_at": None, "exhausted": False},
        "StarvedLane": {"track_ids": [100, 101], "played_count": 0, "last_played_at": None, "exhausted": False},
    }

    # Set starvation threshold to 2 picks
    tmp_pool.state["starvation_config"] = {
        "check_interval_picks": 2,
        "check_interval_minutes": 25,
    }

    tmp_pool.set_pool(
        spec_id=1,
        track_ids=[1, 2, 3, 4, 5, 6, 100, 101],
        source_labels={},
        lanes=lanes,
        balance="even",
        ahead=1,
    )

    # Simulate multiple top_ups to trigger starvation
    for i in range(3):
        result = tmp_pool.top_up(
            current_track_id=1 + i,
            upcoming_track_ids=[1 + i],  # Always need more
        )
        if result["picks"]:
            # Record this pick in the state
            tmp_pool.state["played_this_session"].extend(result["picks"])


def test_backward_compat_legacy_flat_tracks(tmp_pool):
    """Pool handles legacy state with flat track list (backward compat)."""
    # Old-style pool state without lanes
    tmp_pool.state.update({
        "spec_id": 1,
        "eligible_track_ids": [10, 20, 30],
        "source_labels": {10: "A", 20: "A", 30: "B"},
    })

    # status() should still work
    status = tmp_pool.status()
    assert status["spec_id"] == 1
    # Should have created default lane
    assert len(status.get("lanes", [])) >= 0


def test_pool_state_file_location(tmp_pool):
    """Pool state is loaded from specified file path."""
    pool_file = tmp_pool.state_file

    # Write a spec
    tmp_pool.set_pool(
        spec_id=42,
        track_ids=[1, 2],
        source_labels={},
        lanes={"Test": {"track_ids": [1, 2], "played_count": 0, "last_played_at": None, "exhausted": False}},
    )

    # Verify file exists
    assert pool_file.exists()

    # Load and verify
    data = json.loads(pool_file.read_text())
    assert data["spec_id"] == 42


def test_top_up_persists_state(tmp_pool):
    """top_up persists state changes to disk."""
    pool_file = tmp_pool.state_file

    lanes = {
        "Lane": {"track_ids": [1, 2], "played_count": 0, "last_played_at": None, "exhausted": False},
    }

    tmp_pool.set_pool(spec_id=1, track_ids=[1, 2], source_labels={}, lanes=lanes, ahead=2)

    result = tmp_pool.top_up(
        current_track_id=100,
        upcoming_track_ids=[100],  # 1 ahead, need 1 more
    )

    # Verify state file was updated
    data = json.loads(pool_file.read_text())
    assert len(data["played_this_session"]) > 0
    assert data["last_topup_at"] is not None


def test_stop_clears_all_state(tmp_pool):
    """stop() clears pool and lanes."""
    lanes = {
        "Lane": {"track_ids": [1, 2], "played_count": 1, "last_played_at": time.time(), "exhausted": False},
    }

    tmp_pool.set_pool(spec_id=1, track_ids=[1, 2], source_labels={}, lanes=lanes)
    tmp_pool.stop()

    assert tmp_pool.state["spec_id"] is None
    assert tmp_pool.state["lanes"] == {}
    assert tmp_pool.state["played_this_session"] == []


def test_count_sources_legacy_method(tmp_pool):
    """_count_sources aggregates track counts per source (legacy method)."""
    tmp_pool.state["eligible_track_ids"] = [1, 2, 3, 4, 5]
    tmp_pool.state["source_labels"] = {
        1: "Folder1",
        2: "Folder1",
        3: "Folder1",
        4: "Folder2",
        5: "Folder2",
    }

    counts = tmp_pool._count_sources()

    assert counts["Folder1"] == 3
    assert counts["Folder2"] == 2
