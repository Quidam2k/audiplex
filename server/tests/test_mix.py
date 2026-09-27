"""dj_mix planning: Todd's standing order for shuffled mixes (#2842)."""

from audiplex.mix import MixPlan, plan_mix, plan_summary


def test_current_track_never_in_upcoming():
    """The mix must never touch the currently playing item — not by id, and
    not via another folder's copy of the same recording."""
    plan = plan_mix(
        1,
        [],
        [1, 2, 3],
        [1, 4],
        {1: "current", 2: "current", 3: "three", 4: "four"},
        shuffle=False,
    )

    assert plan.upcoming == [3, 4]
    assert plan.trimmed_played == [1, 2]  # each id once


def test_played_tracks_trimmed():
    plan = plan_mix(
        None,
        [1],
        [1, 2, 3],
        [],
        {1: "played", 2: "played", 3: "fresh"},
        shuffle=False,
    )

    assert plan.upcoming == [3]
    assert plan.trimmed_played == [1, 2]


def test_cross_folder_duplicates_collapse():
    plan = plan_mix(
        None,
        [],
        [10],
        [20],
        {10: "rec-a", 20: "rec-a"},
        shuffle=False,
    )

    assert plan.upcoming == [10]
    assert plan.trimmed_duplicates == [20]


def test_id_in_queue_and_new_counts_once():
    plan = plan_mix(None, [], [7], [7], {}, shuffle=False)

    assert plan.upcoming == [7]
    assert plan.kept_from_queue == 1
    assert plan.added == 0
    assert plan.trimmed_duplicates == [7]


def test_no_shuffle_preserves_order():
    plan = plan_mix(None, [], [3, 1], [4, 2], {}, shuffle=False)

    assert plan.upcoming == [3, 1, 4, 2]


def test_shuffle_is_deterministic_for_seed_and_is_a_permutation():
    expected = [1, 2, 3, 4, 5]
    first = plan_mix(None, [], [1, 2, 3], [4, 5], {}, seed=42)
    second = plan_mix(None, [], [1, 2, 3], [4, 5], {}, seed=42)

    assert first.upcoming == second.upcoming
    assert sorted(first.upcoming) == sorted(expected)


def test_empty_everything():
    assert plan_mix(None, [], [], [], {}) == MixPlan([], 0, 0, [], [])


def test_current_none():
    plan = plan_mix(None, [], [1], [2], {}, shuffle=False)

    assert plan.upcoming == [1, 2]
    assert plan.trimmed_played == []


def test_missing_recording_key_is_own_recording():
    plan = plan_mix(None, [], [1, 2], [], {}, shuffle=False)

    assert plan.upcoming == [1, 2]
    assert plan.trimmed_duplicates == []


def test_summary_mentions_counts():
    plan = MixPlan(
        upcoming=[10, 20, 30],
        kept_from_queue=2,
        added=1,
        trimmed_played=[40],
        trimmed_duplicates=[50, 60],
    )

    summary = plan_summary(plan).lower()

    assert summary == (
        "3 upcoming (2 kept, 1 new); trimmed 1 already played, 2 duplicates"
    )

