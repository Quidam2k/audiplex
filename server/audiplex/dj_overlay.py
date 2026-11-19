"""#5695: a DJ break played by the Audiplex app itself, over the end of the song.

An "over" break (dj_prepare) used to go out only through the Pantheon hook
(DJ_OVER_MUSIC_CMD -> speech_clip), so it played on Pantheon's speech path with its
chirps and its chat bubble. A phone build that has the overlay player gets an
`overlay_clip` bus command instead: the stored clip, the track it belongs to, and the
position in that track where it starts. The phone ducks the music, plays the clip on a
second player, and brings the music back. The placement is the same as the Pantheon
hook's (dj_callout_overlay.place): centred on the song's end, inside the outgoing
song's fade plus the next song's quiet intro.

Exactly one path per break: the caller picks overlay OR the hook, never both, and an
overlay the phone drops is not retried anywhere.
"""

from __future__ import annotations

import math
import os
from typing import Optional

OVERLAY_MIN_VERSION_CODE = 84  # #5695 the first app build with the overlay player
DEFAULT_WINDOW = (8.0, 4.0)    # #5695 (outro fade s, next intro quiet s), as dj_over_music.py
START_LATENCY_S = 0.2          # #5695 the phone polls every 100 ms and its player is already prepared
DUCK_TO = 0.35                 # #5695 the music's level under the clip
EXPIRE_AFTER_END_S = 30.0      # #5695 the command is useless once the song is well over


def enabled() -> bool:  # #5695 DJ_OVERLAY=off sends every over break to the Pantheon hook, as before
    return os.environ.get("DJ_OVERLAY", "on").strip().lower() not in ("off", "0", "false", "no")


def phone_has_overlay(state: Optional[dict]) -> bool:  # #5695
    code = (state or {}).get("app_version_code")
    return isinstance(code, int) and code >= OVERLAY_MIN_VERSION_CODE


def _session():  # #5695 tests patch this
    from audiplex import playback_bus

    return playback_bus._pool_session()


def transition_window(out_id: Optional[int], in_id: Optional[int]) -> tuple[float, float]:  # #5695
    """(outro fade, next intro quiet) from track_audio_profile; DEFAULT_WINDOW when unknown
    (the same fallback rule as Pantheon's dj_callout_overlay.transition_window)."""
    try:
        from audiplex.models import TrackAudioProfile

        db = _session()
        if db is None:
            return DEFAULT_WINDOW
        try:
            out = db.get(TrackAudioProfile, out_id) if out_id else None
            fade = out.outro_fade_s if out is not None else None
            if fade is None or not math.isfinite(fade) or fade < 0:
                return DEFAULT_WINDOW
            if in_id is None:
                return float(fade), 0.0
            inc = db.get(TrackAudioProfile, in_id)
            intro = inc.intro_quiet_s if inc is not None else None
            if intro is None or not math.isfinite(intro) or intro < 0:
                return DEFAULT_WINDOW
            return float(fade), float(intro)
        finally:
            db.close()
    except Exception:
        return DEFAULT_WINDOW


def lead_before_end(speech_s: float, fade_s: float, intro_s: float) -> Optional[float]:  # #5695
    """Seconds before the song's end the clip starts; None when it cannot fit the window.
    dj_callout_overlay.place, relative to the end instead of an epoch."""
    try:
        if not all(math.isfinite(v) for v in (speech_s, fade_s, intro_s)):
            return None
        if speech_s <= 0 or min(fade_s, intro_s) < 0 or speech_s > fade_s + intro_s:
            return None
        # release = clamp(end - speech/2, end - fade, end + intro - speech) - latency
        rel = min(max(-speech_s / 2.0, -fade_s), intro_s - speech_s) - START_LATENCY_S
        return -rel
    except Exception:
        return None


def payload(e: dict, state: dict, next_id: Optional[int]) -> Optional[dict]:  # #5695
    """The overlay_clip payload for a ready over-break, or None (no duration, no fit, no
    track length): the caller then uses the Pantheon hook, as before."""
    speech = e.get("clip_duration")
    dur_ms = state.get("duration_ms") or 0
    if not isinstance(speech, (int, float)) or dur_ms <= 0 or e.get("clip_id") is None:
        return None
    fade, intro = transition_window(e.get("for_track"), next_id)
    lead = lead_before_end(float(speech), fade, intro)
    if lead is None:
        return None
    start_at_ms = max(0, int(round(dur_ms - lead * 1000.0)))
    clip_id = int(e["clip_id"])
    return {
        "clip_id": clip_id,
        "clip_url": f"/api/dj/clips/{clip_id}",
        "title": e.get("title") or "DJ break",
        "duration_seconds": float(speech),
        "for_track_id": int(e["for_track"]),
        "start_at_ms": start_at_ms,
        "duck_to": DUCK_TO,
    }
