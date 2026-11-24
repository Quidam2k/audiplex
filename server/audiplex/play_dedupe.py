"""#6837: collapse the duplicate 'start' play stats that extra MediaControllers made.

From October to 1.0.93 the phone sometimes ran two to four player listeners, and
each posted its own 'start' for the same track change, so about 47% of the 'start'
rows in play_stats are copies a second or so apart. The history is left alone
(deleting it is Todd's call); every reader that counts starts collapses them here.
"""
from __future__ import annotations

from typing import Callable, Iterable, TypeVar

T = TypeVar("T")

# Extra listeners fire inside the same track-change callback; a real replay of one
# track inside 3 s is not a thing.
DUP_START_WINDOW_S = 3.0


def collapse_dup_starts(
    items: Iterable[T],
    track: Callable[[T], object],
    event: Callable[[T], str],
    at_s: Callable[[T], float],
) -> list[T]:
    """Drop each 'start' that follows a start of the same track by <= 3 s.

    Chains count (a run of 4 keeps one). Non-start items and the input order are
    untouched. `at_s` returns seconds on any consistent scale.
    """
    items = list(items)
    starts = sorted((i for i, x in enumerate(items) if event(x) == "start"), key=lambda i: at_s(items[i]))
    last: dict[object, float] = {}
    drop: set[int] = set()
    for i in starts:
        key, t = track(items[i]), at_s(items[i])
        if key in last and t - last[key] <= DUP_START_WINDOW_S:
            drop.add(i)
        last[key] = t
    return [x for i, x in enumerate(items) if i not in drop]
