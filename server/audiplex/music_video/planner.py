"""Music-video shot planner (#6172). Pure: no I/O, no Comfy.

One image = one clip. Given the song's duration, beat times and vocal spans,
choose N-1 cut points so the song splits into exactly N clips, cutting on beats
(or gap midpoints) that sit between vocal lines, never mid-lyric. A cut only
lands inside vocals when the song has fewer gaps than cuts; it is then flagged
`forced`.

MiniMax H3 renders on a 17k+5 frame grid at 24 fps (comfy_workflows
agent_gen/h3.py); each clip is rendered at the smallest grid length that covers
its slot and trimmed afterwards in ffmpeg.
"""

import math
from dataclasses import dataclass

FPS = 24
H3_MIN_FRAMES = 90    # 17*5+5; shorter clips are outside H3's useful range
H3_MAX_FRAMES = 362   # top of the trained range (17*21+5)

# sec_per_clip: measured wall time for one 5 s clip on Solace's 4090
# (agent_gen README: ~80 s at 512x288/12 steps, ~200 s at 864x480/20 steps).
QUALITY = {
    "draft": {"width": 512, "height": 288, "steps": 12, "clip_seconds": 5.0, "sec_per_clip": 80},
    "final": {"width": 864, "height": 480, "steps": 20, "clip_seconds": 5.0, "sec_per_clip": 200},
}

VOCAL_MARGIN = 0.15   # a cut this close to a vocal span still counts as mid-lyric
MIN_CLIP = 2.0        # never cut closer than this to the previous cut
MAX_CLIP = 10.0       # never let a clip run longer than this (Todd: 5-10 s clips)
FORCED_COST = 100.0   # one mid-lyric cut outweighs any amount of length drift


@dataclass
class Segment:
    start: float
    end: float
    forced: bool = False  # the cut at `end` had to land inside a vocal span

    @property
    def duration(self) -> float:
        return self.end - self.start


def images_needed(duration: float, clip_seconds: float) -> int:
    return max(1, math.ceil(duration / clip_seconds - 1e-6))


def estimate_render_seconds(n_clips: int, quality: str) -> int:
    return int(n_clips * QUALITY[quality]["sec_per_clip"])


def h3_length(seconds: float) -> int:
    """Smallest H3 frame count (17k+5) that covers `seconds` at FPS."""
    frames = max(H3_MIN_FRAMES, math.ceil(seconds * FPS))
    frames += (5 - frames % 17) % 17
    return min(frames, H3_MAX_FRAMES)


def in_vocals(t: float, vocal_spans, margin: float = VOCAL_MARGIN) -> bool:
    return any(s - margin < t < e + margin for s, e in vocal_spans)


def _gap_midpoints(vocal_spans, duration: float):
    """Midpoints of the silences between (and around) vocal spans."""
    spans = sorted(vocal_spans)
    edges = [0.0] + [x for s, e in spans for x in (s, e)] + [duration]
    mids = []
    for a, b in zip(edges[0::2], edges[1::2]):
        if b - a > 2 * VOCAL_MARGIN:
            mids.append((a + b) / 2)
    return mids


def plan_segments(duration: float, beats, vocal_spans, n: int) -> list[Segment]:
    """Split [0, duration] into exactly n segments, cutting in vocal gaps.

    Candidate cut points are the beats plus the middle of every vocal gap.
    A small dynamic program picks the n-1 cuts that first minimise the number
    of mid-lyric cuts, then keep every clip close to duration/n, with each clip
    between MIN_CLIP and MAX_CLIP seconds.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    step = duration / n
    if n == 1:
        return [Segment(0.0, duration)]
    lo_len, hi_len = min(MIN_CLIP, step), max(MAX_CLIP, 1.5 * step)

    pts = {round(b, 3) for b in beats if 0 < b < duration}
    pts |= {round(g, 3) for g in _gap_midpoints(vocal_spans, duration) if 0 < g < duration}
    pts |= {round(i * step, 3) for i in range(1, n)}  # the even split is always reachable
    cand = sorted(pts)
    forced = [in_vocals(t, vocal_spans) for t in cand]

    def seg_cost(length: float) -> float:
        return ((length - step) / step) ** 2

    INF = float("inf")
    # cost[j], back[k][j]: best plan whose k-th cut (1-based) is at cand[j]
    cost = [seg_cost(t) + FORCED_COST * f if lo_len <= t <= hi_len else INF
            for t, f in zip(cand, forced)]
    back: list[list[int]] = []
    for _k in range(2, n):
        new, bk = [INF] * len(cand), [-1] * len(cand)
        i0 = 0
        for j, t in enumerate(cand):
            while i0 < j and t - cand[i0] > hi_len:
                i0 += 1
            for i in range(i0, j):
                if t - cand[i] < lo_len:
                    break
                c = cost[i] + seg_cost(t - cand[i]) + FORCED_COST * forced[j]
                if c < new[j]:
                    new[j], bk[j] = c, i
        cost = new
        back.append(bk)

    best, last = INF, -1
    for j, t in enumerate(cand):
        tail = duration - t
        if lo_len <= tail <= hi_len and cost[j] + seg_cost(tail) < best:
            best, last = cost[j] + seg_cost(tail), j
    if last < 0:  # infeasible (very short song): fall back to an even split
        cuts = [i * step for i in range(1, n)]
        flags = [in_vocals(t, vocal_spans) for t in cuts]
    else:
        idx = [last]
        for bk in reversed(back):
            idx.append(bk[idx[-1]])
        idx.reverse()
        cuts = [cand[j] for j in idx]
        flags = [forced[j] for j in idx]

    segments, start = [], 0.0
    for cut, f in zip(cuts, flags):
        segments.append(Segment(start, cut, f))
        start = cut
    segments.append(Segment(start, duration))
    return segments
