"""#5867: shuffle a list so versions of one song (same work_id) are never close together."""
from __future__ import annotations

from dataclasses import dataclass
from random import Random
from typing import Iterable


FLOOR_SECONDS = 3600.0


@dataclass(frozen=True)
class ShuffleItem:
    track_id: int
    work_id: str
    duration: float


def _duration_seconds(item: ShuffleItem) -> float:
    duration = item.duration
    if duration is None or duration <= 0:
        return 180.0
    return float(duration)


def gap_seconds(
    items: Iterable[ShuffleItem],
    floor: float = FLOOR_SECONDS,
) -> float:
    """Return max(floor, half the total duration divided by the largest
    number of versions of one work). Empty input returns floor; floor=0
    disables spacing.
    """
    if floor == 0:
        return 0.0

    counts: dict[str, int] = {}
    total_duration = 0.0
    for item in items:
        counts[item.work_id] = counts.get(item.work_id, 0) + 1
        total_duration += _duration_seconds(item)

    if not counts:
        return float(floor)

    return float(max(floor, 0.5 * total_duration / max(counts.values())))


def _insert_at(result: list[ShuffleItem], item: ShuffleItem, start: float) -> float:
    """Insert item so it starts at the first boundary >= start (or at the end).
    Returns the end time of the item in the new list."""
    clock = 0.0
    for index, other in enumerate(result):
        if clock >= start:
            result.insert(index, item)
            return clock + _duration_seconds(item)
        clock += _duration_seconds(other)
    result.append(item)
    return clock + _duration_seconds(item)


def spaced_shuffle(
    items: Iterable[ShuffleItem],
    rng: Random | None = None,
    floor: float = FLOOR_SECONDS,
) -> list[int]:
    """Shuffle tracks so versions of one work stay gap_seconds() apart.

    Works with one track are shuffled as filler. Each work with several
    versions is then placed, version by version, at a random start time at
    least one gap after the previous version and early enough to leave room
    for the rest. Inserting other tracks only widens existing gaps, so earlier
    placements hold. A list too short for the gap degrades to the widest
    spacing that fits. Every input occurrence is kept, duplicates included.
    """
    rng = rng or Random()
    all_items = list(items)
    gap = gap_seconds(all_items, floor)

    groups: dict[str, list[ShuffleItem]] = {}
    for item in all_items:
        groups.setdefault(item.work_id, []).append(item)
    result = [g[0] for g in groups.values() if len(g) == 1]
    rng.shuffle(result)
    multi = [g for g in groups.values() if len(g) > 1]
    rng.shuffle(multi)
    multi.sort(key=len, reverse=True)

    for versions in multi:
        rng.shuffle(versions)
        prev_end: float | None = None
        for placed, item in enumerate(versions):
            total = sum(_duration_seconds(i) for i in result)
            low = 0.0 if prev_end is None else prev_end + gap
            high = total - (len(versions) - placed - 1) * gap
            start = rng.uniform(low, high) if high > low else low
            prev_end = _insert_at(result, item, start)
    return [item.track_id for item in result]
