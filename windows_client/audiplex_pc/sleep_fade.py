"""Pure math for the sleep engine's fade and its crossfade-into-bed mode (#3435).

A line-for-line port of the phone's SleepFade.kt (#1728/#3367) so the PC
renderer fades exactly the way the phone does.
"""
from __future__ import annotations


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def levels(
    i: int,
    steps: int,
    main_start: float,
    bed_start: float,
    bed_target: float | None,
) -> tuple[float, float | None]:
    """Volumes at step i of steps: main ramps linearly from main_start to 0;
    when bed_target is set the bed ramps linearly from bed_start to it over the
    same window. The bed is None when it should be left alone."""
    t = _clamp(i / max(steps, 1))
    main = main_start * (1.0 - t)
    bed = None
    if bed_target is not None:
        bed = _clamp(bed_start + (_clamp(bed_target) - bed_start) * t)
    return main, bed


def resolve_url(url: str, base_url: str) -> str:
    """A relative bed url (e.g. /api/stream/12) resolves against base_url,
    which on the PC is the auth proxy so the Bearer header gets added."""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return base_url.rstrip("/") + "/" + url.lstrip("/")
