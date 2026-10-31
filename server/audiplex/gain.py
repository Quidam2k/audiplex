"""Per-item playback gain in dB (#3504 / #4222), served by GET /api/playback/gain.

Music is trimmed to music_target_lufs and, since #4118, raised by at most
music_max_boost_db (0 = attenuate-only, the pre-#4118 math; a raise needs the
app's limiter). Books are RAISED (never cut) to the voice level (Todd 10/9:
voice gain is never cut), up to a clamp, because a gain above 1.0 needs a
limiter on the phone. Unmeasured items get None: no gain change (#7387).
"""

from __future__ import annotations

MIN_GAIN_DB = 20 * -1.30103  # LoudnessGain's 0.05 floor, about -26 dB


def music_gain_db(lufs: float | None, target: float, max_boost_db: float = 0.0) -> float | None:
    if lufs is None:
        return None
    return round(max(MIN_GAIN_DB, min(max(0.0, max_boost_db), target - lufs)), 2)  # #4118


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


def book_gain_db(lufs: float | None, target: float, max_boost_db: float) -> float | None:
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
    return round(max(0.0, min(max_boost_db, target - lufs)), 2)
