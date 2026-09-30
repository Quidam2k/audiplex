"""Order tracks so each hand-off is in a compatible key near the same tempo (#1002).

Tempo and key are measured (scripts/measure_tempo_key.py), never guessed, so a
track missing either is left out and counted. Pure: no DB, no I/O.

Camelot wheel: each key is a number 1-12 plus B (major) or A (minor). Moves a
DJ's ear accepts: same code; the relative major/minor (8A <-> 8B); one step
round the wheel (8A -> 7A/9A); two steps up (an "energy boost", weaker).

Tempo: two songs match when their BPMs are within `bpm_tolerance` (default 6%),
also counting half/double time (70 next to 140 is fine).

The chain is greedy: start somewhere, then always take the best-scoring
remaining track. With `arc`, each track also has a place on that energy arc
(energy_arc.order_arc) and the chain is pulled toward the arc's shape as the
set plays out.
"""

from __future__ import annotations

import functools
import math
import random
import re

from audiplex.energy_arc import ARCS, order_arc

_CODE = re.compile(r"^(1[0-2]|[1-9])([AB])$")
SMOOTH_KEY = 0.9  # a hand-off is "smooth" at this key score or better, with tempo in tolerance


def parse(code: str | None) -> tuple[int, str] | None:
    m = _CODE.match((code or "").strip().upper())
    return (int(m.group(1)), m.group(2)) if m else None


@functools.lru_cache(maxsize=1024)
def key_score(a: str | None, b: str | None) -> float:
    """1 same key, 0.9 relative or one step, 0.6 two steps up, 0 otherwise."""
    pa, pb = parse(a), parse(b)
    if not pa or not pb:
        return 0.0
    (na, la), (nb, lb) = pa, pb
    step = (nb - na) % 12
    if la == lb:
        return {0: 1.0, 1: 0.9, 11: 0.9, 2: 0.6}.get(step, 0.0)
    return 0.9 if step == 0 else 0.0


def bpm_score(a: float | None, b: float | None, tolerance: float = 0.06) -> float:
    """1 at the same tempo (or half/double), falling to 0 at `tolerance` apart."""
    if not a or not b or a <= 0 or b <= 0:
        return 0.0
    diff = min(abs(math.log(b / a * f)) for f in (0.5, 1.0, 2.0))
    tol = math.log(1 + max(0.005, tolerance))
    return max(0.0, 1.0 - diff / tol)


def order_harmonic(
    tracks: list[tuple[int, float, str, float, int | None]],
    minutes: float = 0,
    start_id: int | None = None,
    bpm_tolerance: float = 0.06,
    arc: str | None = None,
    seed: int | None = None,
) -> dict:
    """tracks: (id, bpm, camelot, seconds, energy|None).

    Returns {ordered, transitions: [{key, bpm}], smooth: n} where each
    transition scores the hand-off into the next track.
    """
    if arc and arc not in ARCS:
        raise ValueError(f"Unknown arc '{arc}'. Use {', '.join(ARCS)}.")
    pool = {t[0]: t for t in tracks if t[1] and parse(t[2])}
    if not pool:
        return {"ordered": [], "transitions": [], "smooth": 0}
    rng = random.Random(seed)
    # Where each track sits on the arc, 0..1 (tracks without energy sit mid-arc).
    place: dict[int, float] = {}
    if arc:
        with_energy = [(t[0], t[4], t[3]) for t in pool.values() if t[4] is not None]
        ranked = order_arc(with_energy, arc, 0, seed)
        n = max(1, len(ranked) - 1)
        place = {tid: i / n for i, tid in enumerate(ranked)}
    total = minutes * 60 if minutes > 0 else sum(t[3] or 0 for t in pool.values())
    jitter = {tid: rng.random() * 1e-3 for tid in pool}  # seeded tie-break

    if start_id in pool:
        cur = start_id
    elif arc:
        cur = min(pool, key=lambda t: place.get(t, 0.5) + jitter[t])
    else:
        cur = rng.choice(sorted(pool))
    info = {tid: (t[1], t[2]) for tid, t in pool.items()}
    ordered, transitions = [cur], []
    elapsed = pool[cur][3] or 0
    del pool[cur]
    while pool and (minutes <= 0 or elapsed < minutes * 60):
        a_bpm, a_key = info[ordered[-1]]
        best, best_cost, best_ks, best_bs = None, math.inf, 0.0, 0.0
        for tid, (_, b_bpm, b_key, sec, _e) in pool.items():
            ks = key_score(a_key, b_key)
            bs = bpm_score(a_bpm, b_bpm, bpm_tolerance)
            cost = (1 - ks) + (1 - bs)
            if arc:
                cost += 2 * abs(place.get(tid, 0.5) - min(1.0, elapsed / total if total else 1))
            cost += jitter[tid]
            if cost < best_cost:
                best, best_cost, best_ks, best_bs = tid, cost, ks, bs
        ordered.append(best)
        transitions.append({"key": round(best_ks, 2), "bpm": round(best_bs, 2)})
        elapsed += pool[best][3] or 0
        del pool[best]
    return {"ordered": ordered, "transitions": transitions,
            "smooth": sum(1 for t in transitions if t["key"] >= SMOOTH_KEY and t["bpm"] > 0)}
