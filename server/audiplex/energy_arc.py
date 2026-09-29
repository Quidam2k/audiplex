"""Order tracks into an energy arc for a DJ set (#2806).

Energy is measured (scripts/measure_energy.py), never inferred, so a track
without a value is left out and counted, not guessed. Pure: no DB, no I/O.

Arcs:
  rise       low to high, for a warm-up or the climb of a ride
  peak       builds to the top about two thirds in, then comes down
  wind_down  high to low
  steady     the tracks closest to the middle energy, shuffled
"""

import random
import statistics

ARCS = ("rise", "peak", "wind_down", "steady")


def _fit(tracks: list[tuple[int, int, float]], minutes: float) -> list[tuple[int, int, float]]:
    """Take tracks in the given order until the set is `minutes` long (0 = all)."""
    if minutes <= 0:
        return list(tracks)
    out, total = [], 0.0
    for t in tracks:
        if total >= minutes * 60:
            break
        out.append(t)
        total += t[2] or 0
    return out


def order_arc(
    tracks: list[tuple[int, int, float]],
    arc: str,
    minutes: float = 0,
    seed: int | None = None,
) -> list[int]:
    """Track ids ordered for `arc`. tracks: (id, energy 0-100, seconds).

    With `minutes`, a random sample of the candidates (or, for steady, the
    ones nearest the median) fills the time first, then gets ordered, so a
    60-minute rise still spans low to high rather than the 15 quietest songs.
    """
    if arc not in ARCS:
        raise ValueError(f"Unknown arc '{arc}'. Use {', '.join(ARCS)}.")
    if not tracks:
        return []
    rng = random.Random(seed)
    pool = list(tracks)
    if arc == "steady":
        mid = statistics.median(t[1] for t in pool)
        rng.shuffle(pool)  # ties at the same distance come out in random order
        pool.sort(key=lambda t: abs(t[1] - mid))
        chosen = _fit(pool, minutes)
        rng.shuffle(chosen)
        return [t[0] for t in chosen]
    rng.shuffle(pool)
    chosen = sorted(_fit(pool, minutes), key=lambda t: t[1])
    if arc == "wind_down":
        chosen.reverse()
    elif arc == "peak":
        up = [t for i, t in enumerate(chosen) if i % 3 != 2]
        down = [t for i, t in enumerate(chosen) if i % 3 == 2]
        chosen = up + down[::-1]
    return [t[0] for t in chosen]
