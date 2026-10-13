"""Per-item playback gain in dB (#3504 / #4222), served by GET /api/playback/gain.

Music is attenuate-only, the same math as the phone's LoudnessGain (#7109), so
serving it changes nothing. Books are RAISED to the voice level (Todd 10/9:
voice gain is never cut), up to a clamp, because a gain above 1.0 needs a
limiter on the phone. Unmeasured items get None: no gain change (#7387).
"""

from __future__ import annotations

MIN_GAIN_DB = 20 * -1.30103  # LoudnessGain's 0.05 floor, about -26 dB


def music_gain_db(lufs: float | None, target: float) -> float | None:
    if lufs is None:
        return None
    return round(max(MIN_GAIN_DB, min(0.0, target - lufs)), 2)


def book_gain_db(lufs: float | None, target: float, max_boost_db: float) -> float | None:
    if lufs is None:
        return None
    return round(max(MIN_GAIN_DB, min(max_boost_db, target - lufs)), 2)
