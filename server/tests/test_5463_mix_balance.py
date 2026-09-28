"""Regression tests for #5463: 992 Ren Faire tracks queued ahead of 277 YouTube tracks."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from audiplex_mcp.mix_balance import balance_order, describe_head, source_counts


def source_map(a_ids, b_ids):
    return {**{item: "A" for item in a_ids}, **{item: "B" for item in b_ids}}


@pytest.mark.parametrize("mode", ["even", "proportional", "none"])
def test_balance_order_is_a_permutation(mode):
    ids = list(range(1, 51))
    sources = {item: "A" if item <= 40 else "B" for item in ids}

    result = balance_order(ids, sources, mode=mode, seed=3)

    assert sorted(result) == sorted(ids)


def test_even_alternates_two_sources():
    a_ids = list(range(100))
    b_ids = list(range(100, 120))
    sources = source_map(a_ids, b_ids)

    result = balance_order(a_ids + b_ids, sources, mode="even", seed=1)
    head = result[:40]

    assert sum(sources[item] == "B" for item in head) == 20
    for k in range(20):
        assert {sources[item] for item in head[2 * k : 2 * k + 2]} == {"A", "B"}


def test_even_balances_three_sources():
    a_ids = list(range(30))
    b_ids = list(range(30, 40))
    c_ids = list(range(40, 45))
    sources = {
        **{item: "A" for item in a_ids},
        **{item: "B" for item in b_ids},
        **{item: "C" for item in c_ids},
    }

    result = balance_order(a_ids + b_ids + c_ids, sources, mode="even", seed=1)

    assert source_counts(result[:15], sources) == {"A": 5, "B": 5, "C": 5}


def test_proportional_distribution():
    a_ids = list(range(80))
    b_ids = list(range(80, 100))
    sources = source_map(a_ids, b_ids)

    result = balance_order(a_ids + b_ids, sources, mode="proportional", seed=2)

    for start in range(0, 100, 10):
        b_count = sum(sources[item] == "B" for item in result[start : start + 10])
        assert 1 <= b_count <= 3


def test_none_mode_is_deterministic_for_same_seed():
    ids = list(range(20))
    sources = {item: "A" if item < 10 else "B" for item in ids}

    assert balance_order(ids, sources, mode="none", seed=7) == balance_order(
        ids, sources, mode="none", seed=7
    )


def test_even_seed_behavior():
    ids = list(range(60))
    sources = {item: "A" if item < 30 else "B" for item in ids}

    first = balance_order(ids, sources, mode="even", seed=1)
    repeated = balance_order(ids, sources, mode="even", seed=1)
    different = balance_order(ids, sources, mode="even", seed=2)

    assert first == repeated
    assert first != different


def test_unlabeled_ids_are_already_queued():
    assert source_counts([1, 2, 3], {1: "A"}) == {"A": 1, "already queued": 2}


def test_invalid_mode_raises_value_error():
    with pytest.raises(ValueError):
        balance_order([1], {1: "A"}, mode="invalid", seed=1)


def test_empty_list_returns_empty():
    assert balance_order([], {}, mode="even", seed=1) == []


def test_describe_head_reports_alternating_sources():
    ids = [1, 2, 3, 4]
    sources = {1: "A", 2: "B", 3: "A", 4: "B"}

    assert "2 A, 2 B" in describe_head(ids, sources, n=4)

