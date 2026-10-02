"""Music-video shot planner (#6172). Pure: no I/O, no Comfy.

One image = one clip. Given the song's duration, beat times and vocal spans,
plan_variable (#6867) picks both how many clips and where to cut: every clip is
VAR_MIN-VAR_MAX seconds (H3's real range), cuts land on beats or gap midpoints
between vocal lines, never mid-lyric. A cut only lands inside vocals when no
plan avoids it; it is then flagged `forced`. plan_segments (fixed n) is kept for
jobs queued before #6867.

MiniMax H3 renders on a 17k+5 frame grid at 24 fps (comfy_workflows
agent_gen/h3.py); each clip is rendered at the smallest grid length that covers
its slot and trimmed afterwards in ffmpeg.
"""

import math
from dataclasses import dataclass

FPS = 24
H3_MIN_FRAMES = 90    # 17*5+5; shorter clips are outside H3's useful range
H3_MAX_FRAMES = 362   # top of the trained range (17*21+5)

# sec_per_frame: H3 render time is linear in frames on Solace's 4090
# (comfy_workflows memory project_h3_progress_eta: ~1.6 s/frame at 480p/20 steps,
# measured over 32 runs; draft 512x288/12 steps ~0.65 s/frame).
QUALITY = {
    "draft": {"width": 512, "height": 288, "steps": 12, "clip_seconds": 5.0, "sec_per_frame": 0.65},
    "final": {"width": 864, "height": 480, "steps": 20, "clip_seconds": 5.0, "sec_per_frame": 1.6},
}

VAR_MIN = 5.0         # #6867: H3's trained range is 124-362 frames = 5.2-15.1 s
VAR_MAX = 15.0
VAR_TARGET = 8.0      # with no lyric reason to cut, clips settle near this
GRID_STEP = 0.5       # fallback cut points, so a 5-15 s plan always exists
OFFBEAT_COST = 3.0    # a grid point that isn't a beat or vocal gap

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


def clip_render_seconds(seconds: float, quality: str) -> int:
    return int(h3_length(seconds) * QUALITY[quality]["sec_per_frame"])


def estimate_render_seconds(segments, quality: str) -> int:
    """Total render time for a plan: frames rendered x seconds per frame."""
    return sum(clip_render_seconds(s.duration, quality) for s in segments)


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


def plan_variable(duration: float, beats, vocal_spans, *, min_len: float = VAR_MIN,
                  max_len: float = VAR_MAX, target: float = VAR_TARGET) -> list[Segment]:
    """Split [0, duration] into clips of min_len-max_len seconds; the count follows the song.

    Candidates are beats, vocal-gap midpoints and a GRID_STEP grid (the grid only
    guarantees a plan exists; it costs OFFBEAT_COST). A DP over candidates
    minimises: mid-lyric cuts first (FORCED_COST each), then off-beat grid cuts,
    then each clip's squared distance from `target`.
    """
    if duration <= max_len:
        return [Segment(0.0, duration)]
    musical = {round(b, 3) for b in beats if 0 < b < duration}
    musical |= {round(g, 3) for g in _gap_midpoints(vocal_spans, duration) if 0 < g < duration}
    grid = {round(i * GRID_STEP, 3) for i in range(1, int(duration / GRID_STEP) + 1)
            if 0 < i * GRID_STEP < duration}
    cand = [0.0] + sorted(musical | grid) + [duration]
    point_cost = [0.0] + [FORCED_COST * in_vocals(t, vocal_spans) + (0 if t in musical else OFFBEAT_COST)
                          for t in cand[1:-1]] + [0.0]

    def seg_cost(length: float) -> float:
        return ((length - target) / target) ** 2

    INF = float("inf")
    best = [INF] * len(cand)
    back = [-1] * len(cand)
    best[0] = 0.0
    i0 = 0
    for j in range(1, len(cand)):
        t = cand[j]
        while t - cand[i0] > max_len + 1e-9:
            i0 += 1
        for i in range(i0, j):
            length = t - cand[i]
            if length < min_len - 1e-9:
                break
            if best[i] == INF:
                continue
            c = best[i] + seg_cost(length) + point_cost[j]
            if c < best[j]:
                best[j], back[j] = c, i
    if back[-1] < 0:  # can't happen with the grid, but never return nothing
        n = max(1, math.ceil(duration / max_len))
        return plan_segments(duration, beats, vocal_spans, n)

    idx = [len(cand) - 1]
    while idx[-1] != 0:
        idx.append(back[idx[-1]])
    idx.reverse()
    segments = []
    for a, b in zip(idx, idx[1:]):
        forced = b != len(cand) - 1 and in_vocals(cand[b], vocal_spans)
        segments.append(Segment(cand[a], cand[b], forced))
    return segments


def plan_dict(segments, quality: str) -> dict:
    """What the UI shows before picking: image count, per-clip range, render estimate."""
    lens = [s.duration for s in segments]
    return {
        "n_images": len(segments),
        "clip_min": round(min(lens), 1), "clip_max": round(max(lens), 1),
        "forced_cuts": sum(s.forced for s in segments),
        "est_render_seconds": estimate_render_seconds(segments, quality),
        "clip_render_min": clip_render_seconds(min(lens), quality),
        "clip_render_max": clip_render_seconds(max(lens), quality),
        "segments": [{"start": round(s.start, 3), "end": round(s.end, 3), "forced": s.forced}
                     for s in segments],
    }
