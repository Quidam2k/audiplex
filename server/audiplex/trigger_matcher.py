"""Cue trigger matching for DJ pool (#5470, #5473, #5477).

Matches playback events against cue triggers to find cues to fire
(patter announcements, track injections, etc).
"""

from typing import Any


def match_triggers(
    event: dict[str, Any],
    cues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find cues matching a playback event.

    event: {kind: "track_end"|"track_start", track_id: <int>, ...}
    cues: [{id, trigger: {kind, track_id}, play_track?, say?, ...}, ...]

    Returns: list of matching cues (caller marks done/fired/etc)
    """
    event_kind = event.get("kind")
    event_track_id = event.get("track_id")

    if not event_kind or event_track_id is None:
        return []

    matches = []
    for cue in cues:
        if cue.get("done"):
            continue  # Already fired
        trigger = cue.get("trigger", {})
        if not isinstance(trigger, dict):
            continue

        trig_kind = trigger.get("kind")
        trig_track_id = trigger.get("track_id")

        # Match if event kind and track match
        if trig_kind == event_kind and trig_track_id == event_track_id:
            matches.append(cue)

    return matches
