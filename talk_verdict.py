"""Is Todd talking? Audiplex's one reader of Pantheon's answer (#7029).

Pantheon's speech-state writer publishes the verdict as the `todd_talking` key of
data/runtime/speech_state.json (Pantheon src/todd_talking.py decides it). Every Audiplex
guard (music starts, DJ breaks, cue holds, swaps) reads it through here, so a fix lands in
one place. This mirrors Pantheon's todd_talking.read(); it is copied, not imported, because
Audiplex also runs on boxes without Pantheon.

- The Talk latch (talk_active) is never talking: a hot mic left on is not speech (#7028).
- No key (a Pantheon writer from before #7029): stt_active or composing, the old rule.
- A file or key older than STALE_S is a dead writer and is never talking.
"""  # #7029

from __future__ import annotations

import time
from datetime import datetime, timezone

KEY = "todd_talking"  # #7029
STALE_S = 5.0  # #7029 the writer ticks every 250 ms


def _ts(value) -> float | None:  # #7029
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:  # naive = UTC (Pantheon's convention), never local time
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def is_fresh(state: dict, now: float | None = None) -> bool:  # #7029
    """False when the file's updated_at is older than STALE_S (a dead writer). No stamp = fresh (old fixtures)."""
    now = time.time() if now is None else now
    at = _ts(state.get("updated_at")) if isinstance(state, dict) else None
    return at is None or now - at <= STALE_S


def is_talking(state: dict, now: float | None = None) -> bool:  # #7029
    """True while Todd is talking or typing, from a parsed speech_state dict."""
    now = time.time() if now is None else now
    if not isinstance(state, dict):
        return False
    if not is_fresh(state, now):  # dead writer
        return False
    key = state.get(KEY)
    if isinstance(key, dict) and "talking" in key:
        key_at = _ts(key.get("at"))
        if key_at is not None and now - key_at > STALE_S:
            return False
        return bool(key["talking"])
    return bool(state.get("stt_active") or state.get("composing"))  # pre-#7029 writer
