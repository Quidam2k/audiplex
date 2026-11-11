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


def spaced_shuffle(
    items: Iterable[ShuffleItem],
    rng: Random | None = None,
    floor: float = FLOOR_SECONDS,
) -> list[int]:
    """Shuffle tracks so versions of one work stay gap_seconds() apart.

    Works with one track are shuffled as filler and laid on a time axis. Each
    work with several versions then gets random positions on that axis, each
    one step (gap + that version + the longest filler, so rounding to a filler
    boundary cannot eat the gap) after the previous and early enough to leave
    room for the rest; everything is sorted by position. Other versions
    landing in between only widen a gap. A list too short for the gap spreads
    the versions evenly instead. O(n log n). Every input occurrence is kept,
    duplicates included.
    """
    rng = rng or Random()
    all_items = list(items)
    gap = gap_seconds(all_items, floor)

    groups: dict[str, list[ShuffleItem]] = {}
    for item in all_items:
        groups.setdefault(item.work_id, []).append(item)
    fillers = [g[0] for g in groups.values() if len(g) == 1]
    rng.shuffle(fillers)

    keyed: list[tuple[float, int, int]] = []  # (position, tie-break, track_id)
    clock = 0.0
    for item in fillers:
        keyed.append((clock, 0, item.track_id))
        clock += _duration_seconds(item)
    total = clock
    longest = max((_duration_seconds(i) for i in fillers), default=0.0)

    for versions in groups.values():
        count = len(versions)
        if count < 2:
            continue
        rng.shuffle(versions)
        step = min(gap + longest + max(_duration_seconds(v) for v in versions), total / (count - 1))
        position = None
        for placed, item in enumerate(versions):
            low = 0.0 if position is None else position + step
            high = total - (count - placed - 1) * step
            position = rng.uniform(low, high) if high > low else low
            keyed.append((position, 1, item.track_id))

    keyed.sort(key=lambda k: (k[0], k[1]))
    return [track_id for _, _, track_id in keyed]
