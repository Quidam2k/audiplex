"""Per-item playback gain in dB (#3504 / #4222), served by GET /api/playback/gain.

Music is trimmed to music_target_lufs and, since #4118, raised by at most
music_max_boost_db (0 = attenuate-only, the pre-#4118 math; a raise needs the
app's limiter). Books are RAISED (never cut) to the voice level (Todd 10/9:
voice gain is never cut), up to a clamp, because a gain above 1.0 needs a
limiter on the phone. Unmeasured items get None: no gain change (#7387).
"""

from __future__ import annotations

import math  # #5683

MIN_GAIN_DB = 20 * -1.30103  # LoudnessGain's 0.05 floor, about -26 dB
TRUE_PEAK_CEILING_DBTP = -1.5  # #5683 a raise never pushes the true peak past this
UNKNOWN_PEAK_MAX_BOOST_DB = 6.0  # #5683 an unmeasured peak keeps the pre-#5683 +6 dB cap
LIMITER_BUDGET_DB = 6.0  # #5683 how much the app limiter may shave off peaks (what +6 always did)


def raise_limit_db(max_boost_db: float, true_peak: float | None,
                   ceiling: float = TRUE_PEAK_CEILING_DBTP,
                   unknown_peak_cap: float = UNKNOWN_PEAK_MAX_BOOST_DB,
                   limiter_budget_db: float = LIMITER_BUDGET_DB) -> float:  # #5683
    """How far an item may be RAISED: min(cap, headroom under the true-peak ceiling +
    the limiter budget). A peaky track gets only the budget, a quiet flat one the full
    cap. Budget 0 = the strict ceiling. Quiet library tracks are mostly dynamic (11/9
    sample: 31 of 40 had < 6 dB headroom), so a strict ceiling would raise them LESS
    than the +6 the limiter has carried since #4118. Peak unknown = that old +6."""
    cap = max(0.0, float(max_boost_db or 0.0))
    if true_peak is None:
        return min(cap, max(0.0, unknown_peak_cap))
    return max(0.0, min(cap, max(0.0, ceiling - true_peak) + max(0.0, limiter_budget_db)))


def music_gain_db(lufs: float | None, target: float, max_boost_db: float = 0.0,
                  true_peak: float | None = None, ceiling: float = TRUE_PEAK_CEILING_DBTP,
                  unknown_peak_cap: float = UNKNOWN_PEAK_MAX_BOOST_DB,
                  limiter_budget_db: float = LIMITER_BUDGET_DB) -> float | None:  # #5683
    if lufs is None:
        return None
    limit = raise_limit_db(max_boost_db, true_peak, ceiling, unknown_peak_cap, limiter_budget_db)  # #5683
    return round(max(MIN_GAIN_DB, min(limit, target - lufs)), 2)  # #4118


def album_reference(rows) -> tuple[float, float | None] | None:  # #5683
    """(album LUFS, album true peak) for album gain, or None = use track gain.

    rows: (lufs, true_peak, duration_s, track_number) per track of ONE album. Album
    mode only where the tags are complete: 2+ tracks, every one numbered and
    measured. LUFS is the duration-weighted power mean of the tracks (close to
    measuring the album as one file); the peak is the loudest track's, or None if
    any track's peak is unmeasured."""
    rows = list(rows)
    if len(rows) < 2 or any(l is None or not n or n <= 0 for l, _p, _d, n in rows):
        return None
    weights = [max(1.0, float(d or 0.0)) for _l, _p, d, _n in rows]
    power = sum(w * 10 ** (l / 10) for (l, _p, _d, _n), w in zip(rows, weights)) / sum(weights)
    peaks = [p for _l, p, _d, _n in rows]
    return round(10 * math.log10(power), 2), (None if any(p is None for p in peaks) else max(peaks))


def music_target_lufs(target: float, dj_offset_db: float = 0.0, dj_active: bool = False) -> float:  # #3501
    """The music target as served: target + the DJ offset while a DJ pool runs.
    Cut-only (clamped to [-12, 0]), so a typo can never make the music louder.
    The phone re-reads /api/music/levels every track change (#7528), so a pool
    starting or stopping takes effect from the next track, with no APK change."""
    if not dj_active:
        return target
    return target + max(-12.0, min(0.0, float(dj_offset_db or 0.0)))


def served_music_target(settings) -> float:  # #3501
    """music_target_lufs for the live server: reads the setting and the DJ pool."""
    offset = getattr(settings, "dj_music_offset_db", 0.0)
    if not offset:
        return settings.music_target_lufs
    try:
        from audiplex.dj_pool import get_pool
        active = get_pool().is_active()
    except Exception:
        active = False
    return music_target_lufs(settings.music_target_lufs, offset, active)


def book_gain_db(lufs: float | None, target: float, max_boost_db: float,
                 true_peak: float | None = None, ceiling: float = TRUE_PEAK_CEILING_DBTP,
                 unknown_peak_cap: float = UNKNOWN_PEAK_MAX_BOOST_DB,
                 limiter_budget_db: float = LIMITER_BUDGET_DB) -> float | None:  # #5683
    """Raise-only (#4222): a book is never cut. Books measure ~-16 LUFS, above
    the voices' -20 FILE level, yet sound quieter because the Pantheon app
    boosts speech up to +9.5 dB (TtsBoost), so book_target_lufs has to be the
    voice's level as heard, not -20, before this raises anything.

    #5282: with raises off (max_boost_db 0) books level by cutting instead,
    like music, or they would not be levelled at all."""
    if lufs is None:
        return None
    if max_boost_db <= 0:
        return round(max(MIN_GAIN_DB, min(0.0, target - lufs)), 2)
    limit = raise_limit_db(max_boost_db, true_peak, ceiling, unknown_peak_cap, limiter_budget_db)  # #5683
    return round(max(0.0, min(limit, target - lufs)), 2)
