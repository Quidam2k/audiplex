"""DJ pool: persistent state, top-up on track changes (#5470, #5473, #5477)."""

import json
import sys
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


def test_pool_initializes_empty(tmp_pool):
    """New pool has no spec or tracks."""
    assert tmp_pool.state["spec_id"] is None
    assert tmp_pool.state["eligible_track_ids"] == []


def test_pool_persists_state(tmp_pool):
    """State written to disk is reloaded on new instance."""
    pool_file = tmp_pool.state_file
    tmp_pool.set_pool(
        spec_id=1,
        track_ids=[10, 20, 30],
        source_labels={10: "A", 20: "A", 30: "B"},
    )

    # New instance reads same file
    pool2 = DJPool(state_file=str(pool_file))
    assert pool2.state["spec_id"] == 1
    assert pool2.state["eligible_track_ids"] == [10, 20, 30]


def test_set_pool_updates_state(tmp_pool):
    """set_pool sets all configuration."""
    result = tmp_pool.set_pool(
        spec_id=5,
        track_ids=[1, 2, 3, 4, 5],
        source_labels={1: "A", 2: "A", 3: "B", 4: "B", 5: "C"},
        balance="proportional",
        ahead=6,
        exclude_recent_hours=24,
    )

    assert result["spec_id"] == 5
    assert result["eligible_count"] == 5
    assert result["ready"] is True
    assert tmp_pool.state["balance_mode"] == "proportional"
    assert tmp_pool.state["ahead"] == 6
    assert tmp_pool.state["exclude_recent_hours"] == 24


def test_top_up_no_pool_returns_empty(tmp_pool):
    """top_up with no pool set returns empty picks."""
    result = tmp_pool.top_up(
        current_track_id=1,
        upcoming_track_ids=[1, 2, 3],
    )
    assert result["picks"] == []
    assert "no pool set" in result["reason"]


def test_top_up_enough_tracks_returns_empty(tmp_pool):
    """top_up returns nothing if already ahead tracks queued."""
    tmp_pool.set_pool(
        spec_id=1,
        track_ids=[10, 20, 30, 40, 50],
        source_labels={10: "A", 20: "A", 30: "B", 40: "B", 50: "C"},
        ahead=4,
    )

    # 4 tracks ahead (skip current)
    result = tmp_pool.top_up(
        current_track_id=1,
        upcoming_track_ids=[1, 10, 20, 30, 40],  # 1 is current, 10-40 are after
    )

    assert result["picks"] == []
    assert "already 4 tracks ahead" in result["reason"]


def test_top_up_picks_when_fewer_than_ahead(tmp_pool):
    """top_up adds tracks when fewer than ahead are queued."""
    tmp_pool.set_pool(
        spec_id=1,
        track_ids=[10, 20, 30, 40, 50],
        source_labels={10: "A", 20: "A", 30: "B", 40: "B", 50: "C"},
        ahead=4,
    )

    # Only 2 tracks ahead
    result = tmp_pool.top_up(
        current_track_id=1,
        upcoming_track_ids=[1, 10, 20],
    )

    assert len(result["picks"]) == 2  # Need 4 total, have 2, pick 2 more
    # Picks should be from eligible pool
    assert all(pick in [10, 20, 30, 40, 50] for pick in result["picks"])


def test_top_up_skips_played_this_session(tmp_pool):
    """top_up skips tracks already played in this session."""
    tmp_pool.set_pool(
        spec_id=1,
        track_ids=[10, 20, 30, 40, 50],
        source_labels={10: "A", 20: "A", 30: "B", 40: "B", 50: "C"},
        ahead=4,
        played_this_session=[10, 20],  # Already played
    )

    result = tmp_pool.top_up(
        current_track_id=1,
        upcoming_track_ids=[1, 30],  # Only 1 ahead
    )

    # Should pick from [40, 50] (not 10, 20 which are played)
    assert all(pick not in [10, 20] for pick in result["picks"])
    assert result["picks"]  # Should have picked something


def test_top_up_updates_played_this_session(tmp_pool):
    """top_up adds its picks to played_this_session."""
    tmp_pool.set_pool(
        spec_id=1,
        track_ids=[10, 20, 30, 40, 50],
        source_labels={10: "A", 20: "A", 30: "B", 40: "B", 50: "C"},
        ahead=4,
    )

    result1 = tmp_pool.top_up(
        current_track_id=1,
        upcoming_track_ids=[1, 10],  # Only 1 ahead, need 3 more
    )

    # Second top_up should skip the picks from the first
    result2 = tmp_pool.top_up(
        current_track_id=10,
        upcoming_track_ids=[10, 20],  # Still only 1 ahead
    )

    # result2's picks should not include result1's picks (unless coincidental)
    played = set(tmp_pool.state["played_this_session"])
    assert len(played) > len(result1["picks"])  # Accumulated


def test_stop_clears_pool(tmp_pool):
    """stop() resets pool to empty state."""
    tmp_pool.set_pool(
        spec_id=1,
        track_ids=[10, 20, 30],
        source_labels={10: "A", 20: "A", 30: "B"},
    )

    tmp_pool.stop()

    assert tmp_pool.state["spec_id"] is None
    assert tmp_pool.state["eligible_track_ids"] == []
    assert tmp_pool.state["played_this_session"] == []


def test_status_includes_source_counts(tmp_pool):
    """status() returns per-source track counts."""
    tmp_pool.set_pool(
        spec_id=2,
        track_ids=[1, 2, 3, 4, 5],
        source_labels={1: "Fast", 2: "Fast", 3: "Slow", 4: "Slow", 5: "Slow"},
        ahead=3,
    )

    status = tmp_pool.status()

    assert status["spec_id"] == 2
    assert status["eligible_count"] == 5
    assert status["balance_mode"] == "even"
    assert status["ahead"] == 3
    assert status["source_counts"]["Fast"] == 2
    assert status["source_counts"]["Slow"] == 3


def test_trigger_matcher_finds_matching_cues():
    """match_triggers finds cues matching an event."""
    cues = [
        {
            "id": 1,
            "trigger": {"kind": "track_end", "track_id": 5},
            "play_track": 100,
            "say": "Bridge",
            "done": False,
        },
        {
            "id": 2,
            "trigger": {"kind": "track_start", "track_id": 10},
            "play_track": None,
            "say": "Now playing Fast Folder",
            "done": False,
        },
        {
            "id": 3,
            "trigger": {"kind": "track_end", "track_id": 5},
            "play_track": None,
            "say": None,
            "done": True,  # Already done
        },
    ]

    event = {"kind": "track_end", "track_id": 5}
    matches = match_triggers(event, cues)

    # Should match cue 1 (kind + track match) but not cue 3 (already done)
    # or cue 2 (different kind)
    assert len(matches) == 1
    assert matches[0]["id"] == 1


def test_trigger_matcher_no_match():
    """match_triggers returns empty list when nothing matches."""
    cues = [
        {
            "id": 1,
            "trigger": {"kind": "track_end", "track_id": 5},
            "play_track": 100,
        },
    ]

    event = {"kind": "track_end", "track_id": 999}  # Different track
    matches = match_triggers(event, cues)

    assert matches == []


def test_trigger_matcher_empty_event():
    """match_triggers handles malformed events gracefully."""
    cues = [
        {
            "id": 1,
            "trigger": {"kind": "track_end", "track_id": 5},
        },
    ]

    event = {}  # Missing kind and track_id
    matches = match_triggers(event, cues)

    assert matches == []


def test_top_up_skips_streams_and_dj_breaks(tmp_pool):
    """top_up ignores streams (id=-1) and DJ breaks (id<0) in upcoming."""
    tmp_pool.set_pool(
        spec_id=1,
        track_ids=[10, 20, 30, 40],
        source_labels={10: "A", 20: "A", 30: "B", 40: "B"},
        ahead=4,
    )

    # Current is a real track, upcoming has DJ breaks (id<0) mixed with real tracks
    result = tmp_pool.top_up(
        current_track_id=1,  # Real track
        upcoming_track_ids=[1, -1, -2, 10, 20, -3],  # DJ breaks mixed with real
    )

    # Should count only 10, 20 as real tracks (2 ahead after current)
    # Need 2 more to reach ahead=4
    assert len(result["picks"]) == 2


def test_pool_handles_corrupt_state_file(tmp_pool):
    """Pool gracefully handles corrupt JSON on disk."""
    pool_file = tmp_pool.state_file
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("{ invalid json }")  # Corrupt

    # Should load with default empty state
    pool2 = DJPool(state_file=str(pool_file))
    assert pool2.state["spec_id"] is None
    assert pool2.state["eligible_track_ids"] == []


def test_count_sources_aggregates_per_source(tmp_pool):
    """_count_sources returns correct per-source counts."""
    tmp_pool.state["eligible_track_ids"] = [1, 2, 3, 4, 5]
    tmp_pool.state["source_labels"] = {
        1: "Folder A",
        2: "Folder A",
        3: "Folder A",
        4: "YouTube",
        5: "YouTube",
    }

    counts = tmp_pool._count_sources()

    assert counts["Folder A"] == 3
    assert counts["YouTube"] == 2
